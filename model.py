import math
from scipy.cluster.vq import kmeans2

import torch
from torch import nn, einsum
import torch.nn.functional as F

import pytorch_lightning as pl


class VQVAEQuantize(nn.Module):
    """
    Neural Discrete Representation Learning, van den Oord et al. 2017
    https://arxiv.org/abs/1711.00937

    Follows the original DeepMind implementation
    https://github.com/deepmind/sonnet/blob/v2/sonnet/src/nets/vqvae.py
    https://github.com/deepmind/sonnet/blob/v2/examples/vqvae_example.ipynb
    """
    def __init__(self, num_hiddens, embedding_dim, n_embed):
        super().__init__()

        self.embedding_dim = embedding_dim
        self.n_embed = n_embed

        self.proj = nn.Conv2d(num_hiddens, embedding_dim, 1)
        self.embed = nn.Embedding(n_embed, embedding_dim)

        self.register_buffer('data_initialized', torch.zeros(1))

    def forward(self, z):
        B, C, H, W = z.size()

        # project and flatten out space, so (B, C, H, W) -> (B*H*W, C)
        z_e = self.proj(z)
        z_e = z_e.permute(0, 2, 3, 1) # make (B, H, W, C)
        flatten = z_e.reshape(-1, self.embedding_dim)

        # DeepMind def does not do this but I find I have to... ;\
        if self.training and self.data_initialized.item() == 0:
            print('running kmeans!!') # data driven initialization for the embeddings
            rp = torch.randperm(flatten.size(0))
            kd = kmeans2(flatten[rp[:20000]].data.cpu().numpy(), self.n_embed, minit='points')
            self.embed.weight.data.copy_(torch.from_numpy(kd[0]))
            self.data_initialized.fill_(1)
            # TODO: this won't work in multi-GPU setups

        dist = (
            flatten.pow(2).sum(1, keepdim=True)
            - 2 * flatten @ self.embed.weight.t()
            + self.embed.weight.pow(2).sum(1, keepdim=True).t()
        )
        _, ind = (-dist).max(1)
        ind = ind.view(B, H, W)

        # vector quantization cost that trains the embedding vectors
        z_q = self.embed_code(ind) # (B, H, W, C)
        commitment_cost = 0.25
        diff = commitment_cost * (z_q.detach() - z_e).pow(2).mean() + (z_q - z_e.detach()).pow(2).mean()

        z_q = z_e + (z_q - z_e).detach() # noop in forward pass, straight-through gradient estimator in backward pass
        z_q = z_q.permute(0, 3, 1, 2) # stack encodings into channels again: (B, C, H, W)
        return z_q, diff, ind

    def embed_code(self, embed_id):
        return F.embedding(embed_id, self.embed.weight)


class GumbelQuantize(nn.Module):
    """
    Gumbel Softmax trick quantizer
    Categorical Reparameterization with Gumbel-Softmax, Jang et al. 2016
    https://arxiv.org/abs/1611.01144
    """
    def __init__(self, num_hiddens, embedding_dim, n_embed, straight_through=True):
        super().__init__()

        self.embedding_dim = embedding_dim
        self.n_embed = n_embed

        self.straight_through = straight_through
        self.temperature = 1.0

        self.proj = nn.Conv2d(num_hiddens, n_embed, 1)
        self.embed = nn.Embedding(n_embed, embedding_dim)

    def forward(self, z):

        # force hard = True when we are in eval mode, as we must quantize
        hard = self.straight_through if self.training else True

        logits = self.proj(z)
        soft_one_hot = F.gumbel_softmax(logits, tau=self.temperature, dim=1, hard=hard)
        z_q = einsum('b n h w, n d -> b d h w', soft_one_hot, self.embed.weight)

        # + kl divergence to the prior loss
        kld_scale = 5e-4 # lol. partly because we are lazily using unnormalized mse loss for reconstruction term
        qy = F.softmax(logits, dim=1)
        diff = kld_scale * torch.sum(qy * torch.log(qy * self.n_embed + 1e-10), dim=1).mean()

        ind = soft_one_hot.argmax(dim=1)
        return z_q, diff, ind


class ResBlock(nn.Module):
    def __init__(self, in_channel, channel):
        super().__init__()

        self.conv = nn.Sequential(
            nn.Conv2d(in_channel, channel, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel, in_channel, 1),
        )

    def forward(self, x):
        out = self.conv(x)
        out += x
        out = F.relu(out)
        return out


class VQVAE(pl.LightningModule):

    def __init__(
        self,
        args,
        num_hiddens=128, # default deepmind settings
        num_residual_hiddens=32,
        embedding_dim=64,
        num_embeddings=512,
    ):
        super().__init__()
        in_channel = 3 # rgb

        # architectures follow deepmind's code at https://github.com/deepmind/sonnet/blob/v2/examples/vqvae_example.ipynb
        self.encoder = nn.Sequential(
            nn.Conv2d(in_channel, num_hiddens//2, 4, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(num_hiddens//2, num_hiddens, 4, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(num_hiddens, num_hiddens, 3, padding=1),
            nn.ReLU(),
            ResBlock(num_hiddens, num_residual_hiddens),
            ResBlock(num_hiddens, num_residual_hiddens),
        )

        QuantizerModule = {
            'vqvae': VQVAEQuantize,
            'gumbel': GumbelQuantize,
        }[args.vq_flavor]
        self.quantizer = QuantizerModule(num_hiddens, embedding_dim, num_embeddings)

        self.decoder = nn.Sequential(
            nn.Conv2d(embedding_dim, num_hiddens, 3, padding=1),
            nn.ReLU(),
            ResBlock(num_hiddens, num_residual_hiddens),
            ResBlock(num_hiddens, num_residual_hiddens),
            nn.ConvTranspose2d(num_hiddens, num_hiddens//2, 4, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(num_hiddens//2, in_channel, 4, stride=2, padding=1),
        )

    def forward(self, x):
        z = self.encoder(x)
        z_q, diff, ind = self.quantizer(z)
        x_hat = self.decoder(z_q)
        return x_hat, diff, ind

    def training_step(self, batch, batch_idx):
        x, y = batch # hate that i have to do this here in the model
        x_hat, latent_loss, ind = self.forward(x)
        recon_loss = F.mse_loss(x_hat, x, reduction='mean')
        loss = recon_loss + latent_loss
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch # hate that i have to do this here in the model
        x_hat, latent_loss, ind = self.forward(x)

        # eval cluster perplexity. when perplexity == num_embeddings then all clusters are used exactly equally
        encodings = F.one_hot(ind, self.quantizer.n_embed).float().reshape(-1, self.quantizer.n_embed)
        avg_probs = encodings.mean(0)
        perplexity = (-(avg_probs * torch.log(avg_probs + 1e-10)).sum()).exp()
        cluster_use = torch.sum(avg_probs > 0)
        self.log('val_perplexity', perplexity, prog_bar=True)
        self.log('val_cluster_use', cluster_use, prog_bar=True)

        """
        data variance is fixed, estimated and used by deepmind in their cifar10 example presumably
        to evaluate a proper log probability under a gaussian, except I think they are also
        missing an additional factor of half? Leaving this alone and following their code anyway.
        https://github.com/deepmind/sonnet/blob/v2/examples/vqvae_example.ipynb
        """
        data_variance = 0.06327039811675479
        recon_error = F.mse_loss(x_hat, x, reduction='mean') / data_variance
        self.log('val_recon_error', recon_error, prog_bar=True) # DeepMind converges to 0.056 in 4min 29s wallclock

    def configure_optimizers(self):

        # separate out all parameters to those that will and won't experience regularizing weight decay
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, torch.nn.Conv2d, torch.nn.ConvTranspose2d)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.BatchNorm2d, torch.nn.Embedding)
        for mn, m in self.named_modules():
            for pn, p in m.named_parameters():
                fpn = '%s.%s' % (mn, pn) if mn else pn # full param name

                if pn.endswith('bias'):
                    # all biases will not be decayed
                    no_decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

        # validate that we considered every parameter
        param_dict = {pn: p for pn, p in self.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params), )
        assert len(param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
                                                    % (str(param_dict.keys() - union_params), )

        # create the pytorch optimizer object
        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": 1e-5},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
        ]
        optimizer = torch.optim.AdamW(optim_groups, lr=3e-4, weight_decay=1e-5)

        return optimizer
