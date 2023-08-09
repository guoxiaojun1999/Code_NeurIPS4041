from typing import Optional

import torch
from torch import nn
import torch.nn.functional as F

from torch_geometric.nn import GCNConv


class NormLayer(nn.Module):
    def __init__(self, args, num_hidden):
        super(NormLayer, self).__init__()
        self.args = args
        self.layer_norm = nn.LayerNorm(num_hidden)
        assert self.args.norm_type in ['dcn', 'cn']
    
    def forward(self, x, tau=1.0):
        if self.args.norm_type == 'dcn':
            norm_x = nn.functional.normalize(x, dim=1)
            sim = norm_x.T @ norm_x / tau
            sim = nn.functional.softmax(sim, dim=1)
            x_neg = x @ sim  
            x = (1 + self.args.scale) * x - self.args.scale * x_neg
            x = self.layer_norm(x)
            return x
        if self.args.norm_type == 'cn':
            norm_x = nn.functional.normalize(x, dim=1)
            sim = norm_x @ norm_x.T / tau
            sim = nn.functional.softmax(sim, dim=1)
            x_neg = sim @ x    
            x = (1 + self.args.scale) * x - self.args.scale * x_neg
            x = self.layer_norm(x)
            return x
    

class Encoder(nn.Module):
    def __init__(self, in_channels, hidden_channels, activation, base_model=GCNConv, k=2, skip=False, norm=None):
        super(Encoder, self).__init__()
        self.base_model = base_model

        assert k >= 2
        self.k = k
        self.skip = skip
        self.norm = norm

        if not self.skip:
            self.conv = [base_model(in_channels, hidden_channels).jittable()]
            for _ in range(1, k):
                self.conv.append(base_model(hidden_channels, hidden_channels))
            self.conv = nn.ModuleList(self.conv)

            self.activation = activation
        else:
            self.fc_skip = nn.Linear(in_channels, hidden_channels)
            self.conv = [base_model(in_channels, hidden_channels)]
            for _ in range(1, k):
                self.conv.append(base_model(hidden_channels, hidden_channels))
            self.conv = nn.ModuleList(self.conv)

            self.activation = activation

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor):
        if not self.skip:
            for i in range(self.k):
                x = self.conv[i](x, edge_index)
                if self.norm != None:
                    x = self.norm(x)
                x = self.activation(x)
            return x
        else:
            h = self.conv[0](x, edge_index)
            if self.norm != None:
                h = self.norm(h)
            h = self.activation(h)
            hs = [self.fc_skip(x), h]
            for i in range(1, self.k):
                u = sum(hs)
                h = self.conv[i](u, edge_index)
                if self.norm != None:
                    h = self.norm(h)
                hs.append(self.activation(h))
            return hs[-1]


class GRACE(torch.nn.Module):
    def __init__(self, encoder: Encoder, num_hidden: int, num_proj_hidden: int, tau: float = 0.5):
        super(GRACE, self).__init__()
        self.encoder: Encoder = encoder
        self.tau: float = tau

        self.fc1 = torch.nn.Linear(num_hidden, num_proj_hidden)
        self.fc2 = torch.nn.Linear(num_proj_hidden, num_hidden)

        self.num_hidden = num_hidden

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        return self.encoder(x, edge_index)

    def projection(self, z: torch.Tensor) -> torch.Tensor:
        z = F.elu(self.fc1(z))
        return self.fc2(z)

    def sim(self, z1: torch.Tensor, z2: torch.Tensor):
        z1 = F.normalize(z1)
        z2 = F.normalize(z2)
        return torch.mm(z1, z2.t())

    def semi_loss(self, z1: torch.Tensor, z2: torch.Tensor, loss_type: str):
        if loss_type == 'info':
            f = lambda x: torch.exp(x / self.tau)
            refl_sim = f(self.sim(z1, z1))
            between_sim = f(self.sim(z1, z2))
            return -torch.log(between_sim.diag() / (refl_sim.sum(1) + between_sim.sum(1) - refl_sim.diag()))
        if loss_type == 'align':
            f = lambda x: torch.exp(x / self.tau)
            between_sim = f(self.sim(z1, z2))
            return -torch.log(between_sim.diag())
        if loss_type == 'uniform':
            f = lambda x: torch.exp(x / self.tau)
            refl_sim = f(self.sim(z1, z1))
            between_sim = f(self.sim(z1, z2))
            return -torch.log(1 / (refl_sim.sum(1) + between_sim.sum(1) - refl_sim.diag()))
        raise NotImplementedError
        

    def batched_semi_loss(self, z1: torch.Tensor, z2: torch.Tensor, batch_size: int):
        # Space complexity: O(BN) (semi_loss: O(N^2))
        device = z1.device
        num_nodes = z1.size(0)
        num_batches = (num_nodes - 1) // batch_size + 1
        f = lambda x: torch.exp(x / self.tau)
        indices = torch.arange(0, num_nodes).to(device)
        losses = []

        for i in range(num_batches):
            mask = indices[i * batch_size:(i + 1) * batch_size]
            refl_sim = f(self.sim(z1[mask], z1))  # [B, N]
            between_sim = f(self.sim(z1[mask], z2))  # [B, N]

            losses.append(-torch.log(between_sim[:, i * batch_size:(i + 1) * batch_size].diag()
                                     / (refl_sim.sum(1) + between_sim.sum(1)
                                        - refl_sim[:, i * batch_size:(i + 1) * batch_size].diag())))

        return torch.cat(losses)

    def loss(self, z1: torch.Tensor, z2: torch.Tensor, mean: bool = True, batch_size: Optional[int] = None, loss_type: str = None):
        h1 = self.projection(z1)
        h2 = self.projection(z2)

        if batch_size is None:
            l1 = self.semi_loss(h1, h2, loss_type)
            l2 = self.semi_loss(h2, h1, loss_type)
        else:
            l1 = self.batched_semi_loss(h1, h2, batch_size)
            l2 = self.batched_semi_loss(h2, h1, batch_size)

        ret = (l1 + l2) * 0.5
        ret = ret.mean() if mean else ret.sum()

        return ret


class LogReg(nn.Module):
    def __init__(self, ft_in, nb_classes):
        super(LogReg, self).__init__()
        self.fc = nn.Linear(ft_in, nb_classes)

        for m in self.modules():
            self.weights_init(m)

    def weights_init(self, m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    def forward(self, seq):
        ret = self.fc(seq)
        return ret