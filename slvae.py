import torch
import torch.nn.functional as F
import torch.nn as nn
import numpy as np
import copy
import time
import networkx as nx
import random
import pickle
from scipy.special import softmax
from scipy.sparse import csr_matrix
import ndlib.models.ModelConfig as mc
import ndlib.models.epidemics as ep
import pandas as pd
import argparse
import matplotlib.pyplot as plt

from main.utils import load_dataset, InverseProblemDataset, adj_process
from main.model.model import GNNModel, VAEModel, DiffusionPropagate, Encoder, Decoder
from main.inversemodel import InverseModel, ForwardModel
from main.inference import model_train, inference
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
import matplotlib.pyplot as plt

from torch.utils.data import DataLoader
from torch.optim import Adam, SGD
from torch.utils.data.dataset import Dataset
from torch.autograd import Variable

print('Is GPU available? {}\n'.format(torch.cuda.is_available()))
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

parser = argparse.ArgumentParser(description="SLVAE")
datasets = ['jazz', 'jazz_SI', 'cora_ml', 'cora_ml_SI', 'power_grid', 'power_grid_SI', 'karate', 'karate_SI', 'netscience', 'netscience_SI', 'digg', 'meme', 'digg7556', 'meme7884']
parser.add_argument("-d", "--dataset", default="checkin", type=str,
                    help="one of: {}".format(", ".join(sorted(datasets))))
args = parser.parse_args()


configs = {
    "ndim": 5,
    "batch_size": 8,
    "hidden_dim": 16,
    "latent_dim": 16,
    "gcn_outdim": 64,
    "output_dim": 32,
    "x_dim" : 2810,
    "lr": 2e-3,
    "epoch": 1000
}

with open('data/' + args.dataset + '.SG', 'rb') as f:
    graph = pickle.load(f)

adj, inverse_pairs, prob_matrix = graph['adj'].toarray(), graph['inverse_pairs'], graph['prob'].toarray()

inverse_pairs = np.array(inverse_pairs)

train_set, test_set = torch.utils.data.random_split(inverse_pairs, [len(inverse_pairs)-configs['batch_size'], configs['batch_size']])

train_loader = DataLoader(dataset=train_set, batch_size=configs['batch_size'], shuffle=True, drop_last=False)
test_loader  = DataLoader(dataset=test_set,  batch_size=1, shuffle=False)
z_all = torch.randn([train_set[:].shape[0], 256])
z_loader = DataLoader(dataset=z_all, batch_size=configs['batch_size'], shuffle=True, drop_last=False)

encoder = Encoder(input_dim= inverse_pairs.shape[1], hidden_dim=512, latent_dim=256)
decoder = Decoder(input_dim = 256, latent_dim=512, hidden_dim =256, output_dim = inverse_pairs.shape[1])
vae_model = VAEModel(Encoder=encoder, Decoder=decoder)

gnn_model = GNNModel(input_dim=configs["ndim"], hiddenunits=[64, 64], num_classes=1, prob_matrix=prob_matrix)
propagate = DiffusionPropagate(prob_matrix, niter=2)
model = InverseModel(vae_model, gnn_model, propagate).to(device)

optimizer = Adam(model.parameters(), lr=2e-3)
batch_size = configs['batch_size']

def loss_all(y, y_hat, x, x_hat, log_var, mean):
    forward_loss = F.mse_loss(y_hat, y)
    reproduction_loss = F.binary_cross_entropy(x_hat, x, reduction='sum')
    KLD = -0.5*torch.sum(1+ log_var - mean.pow(2) - log_var.exp())
    total_loss = forward_loss + reproduction_loss + KLD
    return reproduction_loss, KLD, total_loss

model.train()

for epoch in range(100):
    re_overall = 0
    kld_overall = 0
    total_overall = 0

    precision_all = 0
    recall_all = 0

    for batch_idx, data_pair in enumerate(train_loader):
        # input_pair = torch.cat((data_pair[:, :, 0], data_pair[:, :, 1]), 1).to(device)
        x = data_pair[:, :, 0].float().to(device)
        y = data_pair[:, :, 1].to(device)
        optimizer.zero_grad()

        x_hat, mean, log_var, y_hat = model(x, x)

        re_loss, kld, loss = loss_all(y, y_hat, x, x_hat, log_var, mean)

        x_pred = x_hat.cpu().detach()
        x_true = x.cpu().detach()

        for i in range(x_true.shape[0]):
            x_pred[i][x_pred[i] > 0.5] = 1
            x_pred[i][x_pred[i] != 1] = 0
            precision_all += precision_score(x_true[i].cpu().detach().numpy(), x_pred[i].cpu().detach().numpy())
            recall_all += recall_score(x_true[i].cpu().detach().numpy(), x_pred[i].cpu().detach().numpy())

        kld_overall += kld.item() * x_hat.size(0)
        re_overall += re_loss.item() * x_hat.size(0)
        total_overall += loss.item() * x_hat.size(0)

        loss.backward()
        optimizer.step()

    #         del x, y, x_hat, mean, log_var, y_hat, re_loss, kld, loss
    #         torch.cuda.empty_cache()

    print("Epoch: {}".format(epoch + 1),
          # "\tAverage Loss: {:.4f}".format(overall_loss / len(train_set)),
          "\tReconstruction: {:.4f}".format(re_overall / len(train_set)),
          "\tKLD: {:.4f}".format(kld_overall / len(train_set)),
          "\tTotal: {:.4f}".format(total_overall / len(train_set)),
          "\tPrecision: {:.4f}".format(precision_all / len(train_set)),
          "\tRecall: {:.4f}".format(recall_all / len(train_set)),
          )

print("Training Finished!!")

model.eval()

vae_model = model.vae_model
model = ForwardModel(model.gnn_model, model.propagate).to(device)

for param in vae_model.parameters():
    param.requires_grad = False

for param in model.parameters():
    param.requires_grad = False

encoder = vae_model.Encoder
decoder = vae_model.Decoder

def loss_seed_x(x, x_hat, loss_type='mse'):
    if loss_type =='bce':
        return F.binary_cross_entropy(x_hat, x, reduction='mean')
    else:
        return F.mse_loss(x_hat, x)


def loss_inverse(y_true, y_hat, x_hat, f_z_all, BN):
    forward_loss = F.mse_loss(y_hat, y_true)

    log_pmf = []
    for f_z in f_z_all:
        log_likelihood_sum = torch.zeros(1).to(device)
        for i, x_i in enumerate(x_hat[0]):
            temp = torch.pow(f_z[i], x_i) * torch.pow(1 - f_z[i], 1 - x_i).to(torch.double)
            log_likelihood_sum += torch.log(temp)
        log_pmf.append(log_likelihood_sum)

    log_pmf = torch.stack(log_pmf)
    log_pmf = BN(log_pmf.float())

    pmf_max = torch.max(log_pmf)

    pdf_sum = pmf_max + torch.logsumexp(log_pmf - pmf_max, dim=0)

    return forward_loss - pdf_sum, forward_loss


def loss_inverse_initial(y_true, y_hat, x_hat, f_z):
    forward_loss = F.mse_loss(y_hat, y_true)

    pdf_sum = 0

    for i, x_i in enumerate(x_hat[0]):
        temp = torch.pow(f_z[i], x_i) * torch.pow(1 - f_z[i], 1 - x_i).to(torch.double)
        pdf_sum += torch.log(temp)

    return forward_loss - pdf_sum, pdf_sum


def x_hat_initialization(model, x_hat, x_true, x, y_true, f_z_bar, test_id, threshold=0.45, lr=1e-3, epochs=100):
    input_optimizer = Adam([x_hat], lr=lr)

    initial_x, initial_x_f1 = [], []

    for epoch in range(epochs):
        input_optimizer.zero_grad()

        y_hat = model(x_hat)

        loss, pdf_loss = loss_inverse_initial(y_true, y_hat, x_hat, f_z_bar)

        x_pred = x_hat.clone().cpu().detach().numpy()
        # x = x_true.cpu().detach().numpy()

        x_pred[x_pred > threshold] = 1
        x_pred[x_pred != 1] = 0
        precision = precision_score(x[0], x_pred[0])
        recall = recall_score(x[0], x_pred[0])
        f1 = f1_score(x[0], x_pred[0])

        loss.backward()
        input_optimizer.step()

        with torch.no_grad():
            x_hat.clamp_(0, 1)

        print("Test #{} Epoch: {}".format(test_id + 1, epoch + 1),
              "\tTotal Loss: {:.5f}".format(loss.item()),
              "\tpdf Loss: {:.5f}".format(pdf_loss.item()),
              "\tx Loss: {:.5f}".format(loss_seed_x(x_true, x_hat, loss_type='bce').item()),
              "\tPrec: {:.5f}".format(precision),
              "\tRec: {:.5f}".format(recall),
              "\tF1: {:.5f}".format(f1)
              )
        initial_x.append(x_hat)
        initial_x_f1.append(f1)

    return initial_x, initial_x_f1


x_comparison = {}

for test_id, test in enumerate(test_set):
    if test_id == 0:
        train_x = torch.tensor(train_set[:][:, :, 0]).float().to(device)
        train_y = torch.tensor(train_set[:][:, :, 1]).float().to(device)
        x_true = torch.tensor(test[:, 0]).float().unsqueeze(0).to(device)
        x_true = x_true.unsqueeze(-1)
        y_true = torch.tensor(test[:, 1]).float().unsqueeze(0).to(device)

        # print(x_input.shape)
        with torch.no_grad():
            mean, var = encoder(train_x)
            z_all = vae_model.reparameterization(mean, var)
            # Getting \bar z from all the z's
            z_bar = torch.mean(z_all, dim=0)

            f_z_all = decoder(z_all)
            f_z_bar = decoder(z_bar)

            x_hat = torch.sigmoid(torch.randn(f_z_all[:1].shape)).unsqueeze(-1).to(device)

            # x_hat = torch.bernoulli(x_hat)

        x_hat.requires_grad = True
        x = x_true.cpu().detach().numpy()
        # initialization

        print("Getting x initialization")
        initial_x, initial_x_prec = x_hat_initialization(model, x_hat, x_true, x, y_true,
                                                         f_z_bar, test_id, threshold=0.3,
                                                         lr=5e-2, epochs=30)

        with torch.no_grad():
            #             init_x = torch.sigmoid(initial_x[initial_x_prec.index(max(initial_x_prec))])
            init_x = initial_x[initial_x_prec.index(max(initial_x_prec))]
        # init_x = torch.bernoulli(init_x)

        init_x.requires_grad = True

        input_optimizer = Adam([init_x], lr=1e-1)
        BN = nn.BatchNorm1d(1, affine=False).to(device)

        print("Inference Starting...")
        for epoch in range(5):
            input_optimizer.zero_grad()
            y_hat = model(init_x)
            loss, forward_loss = loss_inverse(y_true, y_hat, init_x, f_z_all, BN)
            print(init_x)

            x_pred = init_x.clone().cpu().detach().numpy()

            auc = roc_auc_score(x[0], x_pred[0])

            x_pred[x_pred > 0.5] = 1
            x_pred[x_pred != 1] = 0
            precision = precision_score(x[0], x_pred[0])
            recall = recall_score(x[0], x_pred[0])
            f1 = f1_score(x[0], x_pred[0])
            accuracy = accuracy_score(x[0], x_pred[0])
            loss.backward()
            # print("loss.grad:", torch.sum(init_x.grad))
            input_optimizer.step()

            with torch.no_grad():
                init_x.clamp_(0, 1)
            print("Test #{} Epoch: {:2d}".format(test_id + 1, epoch + 1),
                  "\tTotal Loss: {:.5f}".format(loss.item()),
                  "\tx Loss: {:.5f}".format(loss_seed_x(x_true, init_x, loss_type='bce').item()),
                  "\tPrec: {:.5f}".format(precision),
                  "\tRec: {:.5f}".format(recall),
                  "\tF1: {:.5f}".format(f1),
                  "\tAUC: {:.5f}".format(auc),
                  "\tACC {:.5f}".format(accuracy)
                  )