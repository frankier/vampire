import torch
import numpy as np
import os
from allennlp.models.model import Model
from allennlp.nn.util import get_text_field_mask
from typing import Dict, Optional, List, Any, Tuple
from allennlp.data import Vocabulary
from allennlp.modules import TextFieldEmbedder
from allennlp.modules import Seq2SeqEncoder, FeedForward
from allennlp.nn.util import get_text_field_mask, get_device_of, get_lengths_from_binary_sequence_mask
from allennlp.models.archival import load_archive, Archive
from allennlp.nn import InitializerApplicator
from overrides import overrides
from modules.vae import VAE
from modules.distribution import Distribution
from modules.encoder import Encoder
from modules.decoder import Decoder
from common.util import schedule, compute_bow, log_standard_categorical, check_dispersion, compute_background_log_frequency
from typing import Dict
from allennlp.training.metrics import CategoricalAccuracy, Average
from modules import Classifier


@Model.register("NVRNN")
class NVRNN(Model):

    def __init__(self,
                 vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 latent_dim: int,
                 hidden_dim: int,
                 encoder: Encoder,
                 decoder: Decoder,
                 distribution: Distribution,
                 background_data_path: str = None,
                 update_bg : bool = False,
                 kl_weight_annealing: str = None,
                 word_dropout: float = 0.5,
                 dropout: float = 0.5,
                 classifier: Classifier = None,
                 initializer: InitializerApplicator = InitializerApplicator()) -> None:
        super(NVRNN, self).__init__(vocab)

        self.metrics = {
            'kld': Average(),
            'nll': Average(),
            'elbo': Average(),
        }

        if classifier is not None:
            self.metrics['accuracy'] = CategoricalAccuracy()

        self.vocab_namespace = text_field_embedder.token_embedder_tokens.vocab_namespace

        if background_data_path is not None:
            bg = compute_background_log_frequency(background_data_path, vocab, self.vocab_namespace)
            if update_bg:
                self.bg = torch.nn.Parameter(bg, requires_grad=True)
            else:
                self.bg = torch.nn.Parameter(bg, requires_grad=False)
        else:
            bg = torch.FloatTensor(vocab.get_vocab_size(self.vocab_namespace))
            self.bg = torch.nn.Parameter(bg)
            torch.nn.init.uniform_(self.bg)

        self.pad_idx = vocab.get_token_to_index_vocabulary(self.vocab_namespace)["@@PADDING@@"]
        self.unk_idx = vocab.get_token_to_index_vocabulary(self.vocab_namespace)["@@UNKNOWN@@"]
        self.sos_idx = vocab.get_token_to_index_vocabulary(self.vocab_namespace)["@@start@@"]
        self.eos_idx = vocab.get_token_to_index_vocabulary(self.vocab_namespace)["@@end@@"]
        self.vocab = vocab
        self._embedder = text_field_embedder
        self._masker = get_text_field_mask
        self._dist = distribution
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.embedding_dim = text_field_embedder.token_embedder_tokens.get_output_dim()
        self._encoder = encoder
        self._decoder = decoder
        self.batch_num = 0
        self.dropout = torch.nn.Dropout(dropout)
        self.word_dropout_ = word_dropout
        self.word_dropout = word_dropout
        self.kl_weight_annealing = kl_weight_annealing
        self._classifier = classifier
        self.tie_weights = tie_weights
        
        if self.tie_weights:
            if self.hidden_dim != self.embedding_dim:
                raise ConfigurationError('When using the tied flag, '
                                         'hidden_dim must be equal to embedding_dim')
            self.decoder.weight = self.encoder.weight

        if self._classifier is not None:
            if self._classifier.input == 'theta':
                self._classifier._initialize_classifier_hidden(latent_dim)
            elif self._classifier.input == 'encoder_output':
                self._classifier._initialize_classifier_hidden(self._encoder._architecture.get_output_dim())
            self._classifier._initialize_classifier_out(vocab.get_vocab_size("labels"))
        
        
        
        # we initialize parts of the decoder, classifier, and distribution here so we don't have to repeat
        # dimensions in the config, which can be cumbersome.
        param_input_dim = self._encoder._architecture.get_output_dim()

        if self._classifier is not None:
            if self._classifier.input == 'encoder_output':
                param_input_dim += vocab.get_vocab_size("labels")
        
        self._dist._initialize_params(param_input_dim, latent_dim)
        
        self._decoder._initialize_decoder_out(vocab.get_vocab_size(self.vocab_namespace))

        if kl_weight_annealing is not None:
            self.weight_scheduler = lambda x: schedule(x, kl_weight_annealing)
        else:
            self.weight_scheduler = lambda x: 1
        self._reconstruction_loss = torch.nn.CrossEntropyLoss(ignore_index=self.pad_idx,
                                                              reduction="none")
        initializer(self)

    def _initialize_weights_from_archive(self,
                                         archive: Archive,
                                         freeze_weights: bool = False) -> None:
        """
        Initialize weights from a model archive.

        Params
        ______
        archive : `Archive`
            pretrained model archive
        """
        model_parameters = dict(self.named_parameters())
        archived_parameters = dict(archive.model.named_parameters())
        for item, val in archived_parameters.items():
            new_weights = val.data
            item_sub = ".".join(item.split('.')[1:])
            model_parameters[item_sub].data.copy_(new_weights)
            if freeze_weights:
                item_sub = ".".join(item.split('.')[1:])
                model_parameters[item_sub].requires_grad = False
    
    def _freeze_weights(self) -> None:
        """
        Freeze the weights of the model
        """

        model_parameters = dict(self.named_parameters())
        for item in model_parameters:
            model_parameters[item].requires_grad = False        

    def drop_words(self, tokens: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        # randomly tokens with <unk>
        tokens = tokens['tokens']
        prob = torch.rand(tokens.size()).to(tokens.device)
        prob[(tokens.data - self.sos_idx) * (tokens.data - self.pad_idx) * (tokens.data - self.eos_idx) == 0] = 1
        tokens_with_unks = tokens.clone()
        tokens_with_unks[prob < self.word_dropout] = self.unk_idx
        return {"tokens": tokens_with_unks}

    def forward(self,
                tokens: Dict[str, torch.Tensor],
                targets: Dict[str, torch.Tensor]=None,
                label: torch.IntTensor=None) -> Dict[str, torch.Tensor]:
        """
        Run one step of VAE with RNN decoder
        """
        if not self.training:
            self.weight_scheduler = lambda x: 1.0
            if self.word_dropout_ < 1.0:
                self.word_dropout = 0.0
        else:
            self.weight_scheduler = lambda x: schedule(x, self.kl_weight_annealing)
            self.word_dropout = self.word_dropout_


        output = {}
        batch_size, _ = tokens['tokens'].shape

        encoder_input = self._embedder(tokens)
        
        encoder_input = self.dropout(encoder_input)

        mask = self._masker(tokens)
        
        # encode tokens
        encoder_output = self._encoder(embedded_text=encoder_input, mask=mask)

        # concatenate generated labels and continuous document vecs as input representation
        input_repr = [encoder_output['encoder_output']]

        if self._classifier is not None:
            if self._classifier.input == 'encoder_output':
                clf_output = self._classifier(encoder_output['encoder_output'], label)
                input_repr.append(clf_output['label_repr'])
        
        input_repr = torch.cat(input_repr, 1)

        # use parameterized distribution to compute latent code and KL divergence
        _, kld, theta = self._dist.generate_latent_code(input_repr, n_sample=1)

        if self._classifier is not None:
            if self._classifier.input == 'theta':
                clf_output = self._classifier(theta, label)

        decoder_input = self.drop_words(tokens)
        decoder_input = self._embedder(decoder_input)
        decoder_input = self.dropout(decoder_input)
        # decode using the latent code.
        decoder_output = self._decoder(embedded_text=decoder_input,
                                       mask=mask,
                                       theta=theta,
                                       bg=self.bg)
        
        if targets is not None:
            
            num_tokens = mask.sum().float()
            reconstruction_loss = self._reconstruction_loss(decoder_output['flattened_decoder_output'],
                                                            targets['tokens'].view(-1))

            # compute marginal likelihood
            nll = reconstruction_loss.sum() / num_tokens
            
            kld_weight = self.weight_scheduler(self.batch_num)

            # add in the KLD to compute the ELBO
            kld = kld.to(nll.device) / num_tokens
            
            elbo = (nll + kld * kld_weight).mean()

            if self._classifier is not None:
                elbo += clf_output['loss']

            avg_cos = check_dispersion(theta)

            output = {
                    'loss': elbo,
                    'elbo': elbo,
                    'nll': nll,
                    'kld': kld,
                    'kld_weight': kld_weight,
                    'avg_cos': float(avg_cos.mean()),
                    'perplexity': torch.exp(nll),
                    }

            if self._classifier is not None:
                output['clf_loss'] = clf_output['loss']

            self.metrics["elbo"](output['elbo'])
            self.metrics["kld"](output['kld'])
            self.metrics["kld_weight"] = output['kld_weight']
            self.metrics["nll"](output['nll'])
            self.metrics["perp"] = float(np.exp(self.metrics['nll'].get_metric()))
            self.metrics["cos"] = output['avg_cos']
            
            if self._classifier is not None:
                self.metrics['accuracy'](clf_output['logits'], label)

        output['encoded_docs'] = encoder_output['encoded_docs']
        output['theta'] = theta
        output['decoder_output'] = decoder_output

        self.batch_num += 1
        return output
    
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        output = {}
        for metric_name, metric in self.metrics.items():
            if isinstance(metric, float):
                output[metric_name] = metric
            else:
                output[metric_name] = float(metric.get_metric(reset))
        return output
