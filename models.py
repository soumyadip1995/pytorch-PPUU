import torch
import torch.nn as nn
import torch.legacy.nn as lnn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
from torch.autograd import Variable
import random, pdb, copy, os, math
import utils

####################
# Basic modules
####################

# encodes a sequence of input frames, and optionally an action, to a hidden representation
class encoder(nn.Module):
    def __init__(self, opt, a_size, n_inputs):
        super(encoder, self).__init__()
        self.opt = opt
        self.a_size = a_size
        self.n_inputs = opt.ncond if n_inputs is None else n_inputs
        # frame encoder
        self.f_encoder = nn.Sequential(
            nn.Conv2d(3*self.n_inputs, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature)
        )


        if a_size > 0:
            if self.opt.tie_action == 1:
                self.aemb_size = opt.nfeature
            else:
                self.aemb_size = opt.nfeature*self.opt.h_height*self.opt.h_width

            # action encoder
            self.a_encoder = nn.Sequential(
                nn.BatchNorm1d(a_size),
                nn.Linear(a_size, opt.nfeature),
                nn.BatchNorm1d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.Linear(opt.nfeature, opt.nfeature),
                nn.BatchNorm1d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.Linear(opt.nfeature, self.aemb_size)
            )

    def forward(self, inputs, actions=None):
        bsize = inputs.size(0)
        h = self.f_encoder(inputs.view(bsize, self.n_inputs*3, self.opt.height, self.opt.width))
        if actions is not None:
            a = self.a_encoder(actions.contiguous().view(bsize, self.a_size))
            if self.opt.tie_action == 1:
                h = h + a.view(bsize, self.opt.nfeature, 1, 1).expand(h.size())
            else:
                h = h + a.view(bsize, self.opt.nfeature, self.opt.h_height, self.opt.h_width)
        return h


# decodes a hidden state into a predicted frame
class decoder(nn.Module):
    def __init__(self, opt):
        super(decoder, self).__init__()
        self.opt = opt
        # minor adjustments to make output size same as input
        if self.opt.dataset == 'simulated':
            self.f_decoder = nn.Sequential(
                nn.ConvTranspose2d(opt.nfeature, opt.nfeature, (4, 5), 2, 1),
                nn.BatchNorm2d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.ConvTranspose2d(opt.nfeature, opt.nfeature, (5, 5), 2, (1, 1)),
                nn.BatchNorm2d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.ConvTranspose2d(opt.nfeature, 3, (2, 2), 2, (0, 1))
            )
        elif self.opt.dataset == 'i80':
            self.f_decoder = nn.Sequential(
                nn.ConvTranspose2d(opt.nfeature, opt.nfeature, (4, 4), 2, 1),
                nn.BatchNorm2d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.ConvTranspose2d(opt.nfeature, opt.nfeature, (5, 5), 2, (0, 1)),
                nn.BatchNorm2d(opt.nfeature),
                nn.LeakyReLU(0.2),
                nn.ConvTranspose2d(opt.nfeature, 3, (2, 2), 2, (0, 1))
            )


    def forward(self, h):
        bsize = h.size(0)
        out = self.f_decoder(h)[:, :, :-1].clone()
        return out.view(bsize, 1, 3, self.opt.height, self.opt.width)




# encodes a sequence of frames or errors and produces a distribution over latent variables
class z_network(nn.Module):
    def __init__(self, opt):
        super(z_network, self).__init__()
        self.opt = opt

        self.network = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, 2*opt.nz)
        )

    def reparameterize(self, mu, logvar, sample):
        if self.training or sample:
            std = logvar.mul(0.5).exp_()
            eps = Variable(std.data.new(std.size()).normal_())
            return eps.mul(std).add_(mu)
        else:
            return mu

    def encode(self, inputs):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.nfeature*self.opt.h_height*self.opt.h_width)
        z_params = self.network(inputs).view(-1, self.opt.nz, 2)
        mu = z_params[:, :, 0]
        logvar = z_params[:, :, 1]
        return mu, logvar

    def forward(self, inputs, sample=False):
        mu, logvar = self.encode(inputs)
        z = self.reparameterize(mu, logvar, sample)
        return z, mu, logvar



# encodes a sequence of frames or errors and produces a latent variable
class z_network_det(nn.Module):
    def __init__(self, opt, n_inputs):
        super(z_network_det, self).__init__()
        self.opt = opt
        self.n_inputs = n_inputs

        self.conv = nn.Sequential(
            nn.Conv2d(3*n_inputs, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.BatchNorm2d(opt.nfeature)
        )

        self.fc = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nz),
            nn.Tanh()
        )

    def forward(self, inputs, sample=False):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.n_inputs*3, self.opt.height, self.opt.width)
        z = self.fc(self.conv(inputs).view(bsize, -1))
        return z


# takes as input a sequence of frames, outputs the means and variances of a diagonal Gaussian. 
class u_network_gaussian(nn.Module):
    def __init__(self, opt, n_inputs):
        super(u_network_gaussian, self).__init__()
        self.opt = opt
        self.n_inputs = n_inputs

        self.conv = nn.Sequential(
            nn.Conv2d(3*n_inputs, opt.nfeature, 4, 2, 1),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
        )

        self.fc = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, 2*opt.nz)
        )

    def forward(self, inputs):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.n_inputs*3, self.opt.height, self.opt.width)
        z = self.fc(self.conv(inputs).view(bsize, -1))
        mu = z[:, :self.opt.nz]
        sigma = F.softplus(z[:, self.opt.nz:] )
        return mu, sigma

# predict a vector u the same size as z, which will be use to predict 
# a distribution over z's
class u_network(nn.Module):
    def __init__(self, opt, n_inputs):
        super(u_network, self).__init__()
        self.opt = opt
        self.n_inputs = n_inputs

        self.conv = nn.Sequential(
            nn.Conv2d(3*n_inputs, opt.nfeature, 4, 2, 1),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
            nn.LeakyReLU(0.2),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1)
        )

        if self.opt.loss == 'nll-w':
            n_out = opt.nz + 1
        else:
            n_out = opt.nz

        self.fc = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, n_out)
        )

    def forward(self, inputs):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.n_inputs*3, self.opt.height, self.opt.width)
        u = self.fc(self.conv(inputs).view(bsize, -1))
        if self.opt.u_sphere == 1:
            u = u / torch.norm(u, 2, 1).view(-1, 1).expand(u.size())
        return u


# expands a latent variable to the size of the hidden representation
class z_expander(nn.Module):
    def __init__(self, opt, n_steps):
        super(z_expander, self).__init__()
        self.opt = opt
        self.n_steps = n_steps
        self.z_expander = nn.Sequential(
            nn.Linear(opt.nz, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, n_steps * opt.nfeature * self.opt.h_height * self.opt.h_width)
        )

    def forward(self, z):
        bsize = z.size(0)
        z_exp = self.z_expander(z).view(bsize, self.n_steps, self.opt.nfeature, self.opt.h_height, self.opt.h_width)
        return z_exp




# combines a sequence of images with the state representing absolute speed
class policy_encoder(nn.Module):
    def __init__(self, opt):
        super(policy_encoder, self).__init__()
        self.opt = opt

        self.convnet = nn.Sequential(
            nn.Conv2d(3*opt.ncond, opt.nfeature, 4, 2, 1),
#            nn.BatchNorm2d(opt.nfeature),
            nn.ReLU(),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
#            nn.BatchNorm2d(opt.nfeature),
            nn.ReLU(),
            nn.Conv2d(opt.nfeature, opt.nfeature, 4, 2, 1),
#            nn.BatchNorm2d(opt.nfeature),
            nn.ReLU()
        )

        self.embed = nn.Sequential(
#            nn.BatchNorm1d(opt.ncond*opt.n_inputs),
            nn.Linear(opt.ncond*opt.n_inputs, opt.n_hidden),
#            nn.BatchNorm1d(opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, opt.n_hidden)
#            nn.BatchNorm1d(opt.n_hidden)
        )

        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width

    def forward(self, state_images, states):
        bsize = state_images.size(0)
        state_images = state_images.view(bsize, 3*self.opt.ncond, self.opt.height, self.opt.width)
        states = states.view(bsize, -1)
        hi = self.convnet(state_images).view(bsize, self.hsize)
        hs = self.embed(states)
        h = torch.cat((hi, hs), 1)
        return h


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
        elif t == 'cpu':
            self.cpu()






###############
# Main models
###############

# forward model, deterministic
class FwdCNN(nn.Module):
    def __init__(self, opt, mfile):
        super(FwdCNN, self).__init__()
        self.opt = opt
        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.decoder = pretrained_model.decoder
            self.encoder.n_inputs = opt.ncond


    def forward(self, inputs, actions, target, sampling=None):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)
        pred = []
        for t in range(npred):
            h = self.encoder(inputs, actions[:, t])
            pred_ = F.sigmoid(self.decoder(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        pred = torch.cat(pred, 1)
        return pred, Variable(torch.zeros(1))


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
        elif t == 'cpu':
            self.cpu()



# forward VAE model with a learned prior
class FwdCNN_VAE_FP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_VAE_FP, self).__init__()
        self.opt = opt
        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.decoder = pretrained_model.decoder
            self.encoder.n_inputs = opt.ncond


        self.y_encoder = encoder(opt, 0, 1)
        self.z_network = z_network(opt)
        self.z_expander = z_expander(opt, 1)

    def forward(self, inputs, actions, targets, sampling=None):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        kld = Variable(torch.zeros(1))
        if self.use_cuda:
            kld = kld.cuda()
        pred = []
        for t in range(npred):
            h_x = self.encoder(inputs, actions[:, t])
            if targets is not None:
                # we are training
                h_y = self.y_encoder(targets[:, t].unsqueeze(1).contiguous())
                z, mu, logvar = self.z_network(h_x + h_y)
                logvar = torch.clamp(logvar, max = 4) # this can go to inf when taking exp(), so clamp it
                kld_t = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
#                kld_t = torch.clamp(kld_t, max=50)
#                kld_t = -0.5 * torch.clamp(torch.sum(1 + logvar - mu.pow(2) - logvar.exp()), min=50, max=50)                    
                kld_t /= bsize
                kld += kld_t
                z_exp = self.z_expander(z)
            else:
                # we are generating samples
                z = Variable(torch.randn(bsize, self.opt.nz).cuda())
                z_exp = self.z_expander(z)

            h = h_x + z_exp.squeeze()
            pred_ = F.sigmoid(self.decoder(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        kld /= npred
        pred = torch.cat(pred, 1)
        return pred, kld

    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False




# forward VAE model with a learned prior
class FwdCNN_VAE_LP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_VAE_LP, self).__init__()
        self.opt = opt
        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.decoder = pretrained_model.decoder
            self.encoder.n_inputs = opt.ncond


        self.y_encoder = encoder(opt, 0, 1)
        self.z_network = z_network(opt)
        self.q_network = z_network(opt)
        self.z_expander = z_expander(opt, 1)

    def forward(self, inputs, actions, targets, sampling=None):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        kld = Variable(torch.zeros(1))
        if self.use_cuda:
            kld = kld.cuda()
        pred = []
        for t in range(npred):
            h_x = self.encoder(inputs, actions[:, t])
            if targets is not None:
                # we are training
                h_y = self.y_encoder(targets[:, t].unsqueeze(1).contiguous())
                z1, mu1, logvar1 = self.z_network(h_x + h_y)
                z2, mu2, logvar2 = self.q_network(h_x)
                sigma1 = logvar1.mul(0.5).exp()
                sigma2 = logvar2.mul(0.5).exp()
                kld_t = torch.log(sigma2/sigma1 + 1e-8) + (torch.exp(logvar1) + (mu1 - mu2)**2)/(2*torch.exp(logvar2)) - 1/2
                kld_t = torch.clamp(kld_t, max=50)
                kld_t = torch.sum(kld_t) / bsize
                kld += kld_t
                z_exp = self.z_expander(z1)
            else:
                # we are generating samples
                z, _, _ = self.q_network(h_x, sample=True)
                z_exp = self.z_expander(z)

            h = h_x + z_exp.squeeze()
            pred_ = F.sigmoid(self.decoder(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        kld /= npred
        pred = torch.cat(pred, 1)
        return pred, kld

    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False




# forward EEN model with a fixed prior
class FwdCNN_EEN_FP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_EEN_FP, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            self.mfile = mfile
            print(f'[initializing encoder and decoder with: {mfile}]')
            pretrained_model = torch.load(mfile)
            self.encoder1 = pretrained_model.encoder
            self.decoder1 = pretrained_model.decoder
            self.encoder2 = copy.deepcopy(self.encoder1)
            self.decoder2 = copy.deepcopy(self.decoder1)

        self.z_network_det = z_network_det(opt, 1)
        self.z_expander = z_expander(opt, 1)
        self.p_z = []

    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z).contiguous()
        if self.use_cuda: z = z.cuda()
        return Variable(z)

    def forward(self, inputs, actions, targets, save_z = False, sampling='fp'):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        kld = Variable(torch.zeros(1))
        if self.use_cuda:
            kld = kld.cuda()

        pred = []
        for t in range(npred):
            if targets is not None:
                # we are training or estimating z distribution
                h1 = self.encoder1(inputs, actions[:, t])
                pred1 = F.sigmoid(self.decoder1(h1) + inputs[:, -1].unsqueeze(1).clone())
                error = targets[:, t] - pred1.squeeze()
                error = Variable(error.data)
                z = self.z_network_det(error)
                if save_z:
                    self.save_z(z)
            else:
                # we are doing inference
                z = self.sample_z(bsize)

            z_exp = self.z_expander(z)
            h2 = self.encoder2(inputs, actions[:, t])
            h2 = h2 + z_exp.squeeze()
            pred2 = F.sigmoid(self.decoder2(h2) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred2)
            inputs = torch.cat((inputs[:, 1:], pred2), 1)

        pred = torch.cat(pred, 1)
        return pred, kld


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False



class LSTMCritic(nn.Module):
    def __init__(self, opt):
        super(LSTMCritic, self).__init__()
        self.opt = opt
        self.encoder = encoder(opt, 0, 1)
        self.input_size = opt.nfeature*opt.h_height*opt.h_width
        self.lstm = nn.LSTM(self.input_size, opt.nhidden)
        self.classifier = nn.Linear(opt.nhidden, 1)
        self.hidden = (Variable(torch.randn(1, 2*opt.batch_size, opt.nhidden).cuda()),
                       Variable(torch.randn(1, 2*opt.batch_size, opt.nhidden).cuda()))


    def forward(self, inputs):
        self.hidden = (Variable(torch.randn(1, 2*self.opt.batch_size, self.opt.nhidden).cuda()),
                       Variable(torch.randn(1, 2*self.opt.batch_size, self.opt.nhidden).cuda()))
        inputs.detach()
        bsize = inputs.size(0)
        T = inputs.size(1)
        for t in range(T):
            enc = self.encoder(inputs[:, t].contiguous()).view(1, 2*self.opt.batch_size, -1)
            out, self.hidden = self.lstm(enc, self.hidden)
        logits = self.classifier(out.squeeze())
        return logits.squeeze()
            
        







# forward AE model with a fixed prior
class FwdCNN_AE_FP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_AE_FP, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.decoder = pretrained_model.decoder
            self.encoder.n_inputs = opt.ncond

        self.y_encoder = encoder(opt, 0, 1)

        self.z_network = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nz)
        )

        self.z_expander = z_expander(opt, 1)
        self.p_z = []

    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z).contiguous()
        if self.use_cuda: z = z.cuda()
        return Variable(z)

    def forward(self, inputs, actions, targets, save_z = False, sampling='fp'):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        kld = Variable(torch.zeros(1))
        if self.use_cuda:
            kld = kld.cuda()

        pred = []
        self.Z = []
        self.z_top_list = []
        for t in range(npred):
            h_x = self.encoder(inputs, actions[:, t])
            if targets is not None:
                # we are training or estimating z distribution
                h_y = self.y_encoder(targets[:, t].unsqueeze(1).contiguous())
                z = self.z_network((h_x + h_y).view(bsize, -1))
                if self.opt.z_sphere == 1:
                    z = z / (1e-8 + torch.norm(z, 2, 1).view(-1, 1).expand(z.size()))
                if save_z:
                    self.save_z(z)
            else:
                # we are doing inference
                if sampling == 'fp':
                    z = self.sample_z(bsize)
                elif sampling == 'lp-pdf':
                    mu, sigma = self.q_network(inputs)
                    z = mu + Variable(torch.randn(sigma.size()).cuda()) * sigma
                elif sampling == 'pdf':
                    n_sample=100
                    n_z = bsize*1000
                    if len(self.Z) == 0:
                        self.Z = self.sample_z(n_z)
                    mu, sigma = self.q_network(inputs)
                    mu = mu.contiguous()
                    sigma = sigma.contiguous()
                    Z_exp = self.Z.view(1, n_z, self.opt.nz).expand(bsize, n_z, self.opt.nz)
                    mu_exp = mu.view(bsize, 1, self.opt.nz).expand(bsize, n_z, self.opt.nz)
                    sigma_exp = sigma.view(bsize, 1, self.opt.nz).expand(bsize, n_z, self.opt.nz)
                    mu_exp = mu_exp.contiguous().view(-1, self.opt.nz)
                    sigma_exp = sigma_exp.contiguous().view(-1, self.opt.nz)
                    Z_exp = Z_exp.clone().view(-1, self.opt.nz)
                    z_loss = utils.log_pdf(Z_exp, mu_exp, sigma_exp)
                    z_loss = z_loss.view(bsize, -1)
                    _, z_ind = torch.topk(z_loss, n_sample, dim=1, largest=False)
                    z_top = Variable(self.Z.data.index(z_ind.data.view(-1))).view(bsize, n_sample, self.opt.nz)
                    z_ind = random.choice(z_ind.t())
                    z = Variable(self.Z.data.index(z_ind.data))
                    self.z_top_list.append(z_top.data)

                    if random.random() < 0.01 and False:
                        # save
                        print('[saving]')
                        hsh = random.randint(0, 10000)
                        Z = Z.data.cpu().numpy()
                        z_top = z_top.data.cpu().numpy()
                        embed = utils.embed(Z, z_top)
                        os.system('mkdir -p z_viz/pdf_{}samples'.format(n_sample))
                        torch.save(embed, 'z_viz/pdf_{}samples/{:05d}.pth'.format(n_sample, hsh))
                    del mu, sigma, Z_exp, mu_exp, sigma_exp
                    
                elif sampling == 'sphere':
                    Z = self.sample_z(bsize*500)
                    u = self.q_network(inputs)
                    e = torch.mm(u, Z.t())
                    # sample from 100 closest vectors
                    _, z_ind = torch.topk(e, 100, dim=1)
                    z_ind = random.choice(z_ind.t())
                    z = Variable(Z.data.index(z_ind.data))
                    if random.random() < 0.02:
                        # save
                        print('[saving]')
                        hsh = random.randint(0, 10000)
                        Z = Z.data.cpu().numpy()
                        z_top = z_top.data.cpu().numpy()
                        embed = utils.embed(Z, z_top)
                        torch.save(embed, 'z_viz/{:05d}.pth'.format(hsh))
                elif sampling == 'lp-nll':
                    Z = self.sample_z(bsize*500)
                    u = self.q_network(inputs)
                    if self.opt.z_sphere == 1:
                        u = u / (1e-8 + torch.norm(u, 2, 1).view(-1, 1).expand(u.size()))

                    p = F.softmax(torch.mm(u, Z.t()), dim=1)
                    z_top_indx = torch.multinomial(p, 20)
                    z_top = Variable(Z.data.index(z_top_indx.data.long().view(-1).squeeze())).view(bsize, 20, -1).contiguous()
                    z = z_top[:, 0].contiguous()
                    if random.random() < 0.02:
                        # save
                        print('[saving]')
                        hsh = random.randint(0, 10000)
                        Z = Z.data.cpu().numpy()
                        z_top = z_top.data.cpu().numpy()
                        embed = utils.embed(Z, z_top)
                        torch.save(embed, 'z_viz/{:05d}.pth'.format(hsh))

                if self.opt.z_sphere == 1:
                    z = z / (1e-8 + torch.norm(z, 2, 1).view(-1, 1).expand(z.size()))


            z_exp = self.z_expander(z)
            if self.opt.z_mult == 1:
                h = h_x * z_exp.squeeze()
            else:
                h = h_x + z_exp.squeeze()
            pred_ = F.sigmoid(self.decoder(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        pred = torch.cat(pred, 1)
        return pred, kld

    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False


# forward AE model with a learned prior
class FwdCNN_AE_LP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_AE_LP, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions, opt.ncond)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.decoder = pretrained_model.decoder
            self.encoder.n_inputs = opt.ncond

        self.y_encoder = encoder(opt, 0, 1)

        self.z_network = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nz)
        )

        if self.opt.loss2 == 'nll':
            u_out = opt.nz + 1
        elif self.opt.loss2 == 'mse2':
            u_out = opt.nz
        elif self.opt.loss2 == 'pdf':
            u_out = 2*opt.nz 

        self.u_network = nn.Sequential(
            nn.Linear(opt.nfeature*self.opt.h_height*self.opt.h_width, opt.nfeature),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, opt.nfeature),
            nn.BatchNorm2d(opt.nfeature),
            nn.LeakyReLU(0.2),
            nn.Linear(opt.nfeature, u_out)
        )


        self.z_expander = z_expander(opt, 1)
        self.p_z = []
        self.u_targets = Variable(torch.arange(self.opt.batch_size).long())

    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z)
        if self.use_cuda: z = z.cuda()
        return Variable(z)

    def forward(self, inputs, actions, targets, save_z = False, debug=False):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        kld = Variable(torch.zeros(1))
        self.eye = Variable(torch.eye(bsize))
        if self.use_cuda:
            kld = kld.cuda()
            self.u_targets = self.u_targets.cuda()
            self.eye = self.eye.cuda()

        pred = []
        for t in range(npred):
            h_x = self.encoder(inputs, actions[:, t])
            if targets is not None:
                # we are training or estimating z distribution
                h_y = self.y_encoder(targets[:, t].unsqueeze(1).contiguous())
                z = self.z_network((h_x + h_y).view(bsize, -1))
                if self.opt.loss2 != 'pdf':
                    z = z / torch.norm(z, 2, 1).view(-1, 1).expand(z.size())
                else:
                    pass
#                    z = F.tanh(z)
                if save_z:
                    self.save_z(z)
            else:
                # we are doing inference
                z = self.sample_z(bsize)

            
            us = self.u_network(h_x.view(bsize, -1))
            if self.opt.loss2 == 'nll':
                u = us[:, :-1]
                u = u / torch.norm(u, 2, 1).view(-1, 1).expand(u.size())
                e = torch.mm(u, z.t())
                s = F.softplus(us[:, -1].contiguous())
                e *= s.view(-1, 1).expand(e.size())
                log_p = F.log_softmax(e, dim=1)
                kld += F.nll_loss(log_p, self.u_targets)
            elif self.opt.loss2 == 'mse':
                u = us
                u = u / torch.norm(u, 2, 1).view(-1, 1).expand(u.size())
                e = torch.mm(u, z.t())
                e = torch.abs(e)
                kld += F.mse_loss(e, self.eye)
            elif self.opt.loss2 == 'pdf':
                assert(self.opt.nz % 2 == 0)
                k = int(self.opt.nz/2)
                mu = us[:, :2]
#                mu = F.tanh(us[:, :2])
                sigma = F.softplus(us[:, 2:])
                a = 0.5*torch.sum((z-mu)**2/(1e-8 + (sigma)**2), 1)
                b = torch.log(1e-8 + ((2*math.pi)**k)*torch.prod(sigma, 1))
                if torch.abs(torch.mean(a + b)).data[0] > 100:
                    pdb.set_trace()
                kld += torch.mean(a+b)

            z_exp = self.z_expander(z)
            h = h_x + z_exp.squeeze()
            pred_ = F.sigmoid(self.decoder(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)
            if debug:
                pdb.set_trace()

        pred = torch.cat(pred, 1)
        return pred, kld

    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False







# forward EEN model with a learned prior
class FwdCNN_EEN_LP(nn.Module):
    def __init__(self, opt, mfile=''):
        super(FwdCNN_EEN_LP, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = encoder(opt, opt.n_actions)
            self.decoder = decoder(opt)
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            self.mfile = mfile
            pretrained_model = torch.load(mfile)
            self.encoder1 = pretrained_model.encoder
            self.decoder1 = pretrained_model.decoder
            self.encoder2 = copy.deepcopy(self.encoder1)
            self.decoder2 = copy.deepcopy(self.decoder1)

        self.z_network = z_network(opt, opt.ncond+opt.npred)
        self.q_network = z_network(opt, opt.ncond)
        self.z_expander = z_expander(opt)

    def forward(self, inputs, actions, targets):
        bsize = inputs.size(0)
        inputs = inputs.view(bsize, self.opt.ncond, 3, self.opt.height, self.opt.width)
        actions = actions.view(bsize, -1, self.opt.n_actions)
        npred = actions.size(1)

        # deterministic pass
        pred = []
        for t in range(npred):
            h = self.encoder1(inputs, actions[:, t])
            pred_ = F.sigmoid(self.decoder1(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        pred1 = torch.cat(pred, 1)
        errors = targets - pred1
        errors = Variable(errors.data)

        z, mu1, logvar1 = self.z_network(torch.cat((inputs, errors), 1))
        _, mu2, logvar2 = self.q_network(inputs)
        z_exp = self.z_expander(z)

        # latent pass
        pred = []
        for t in range(npred):
            h = self.encoder2(inputs, actions[:, t])
            h = h + z_exp[:, t]
            pred_ = F.sigmoid(self.decoder2(h) + inputs[:, -1].unsqueeze(1).clone())
            pred.append(pred_)
            inputs = torch.cat((inputs[:, 1:], pred_), 1)

        pred2 = torch.cat(pred, 1)

        sigma1 = logvar1.mul(0.5).exp()
        sigma2 = logvar2.mul(0.5).exp()
        kld = torch.log(sigma2/sigma1) + (torch.exp(logvar1) + (mu1 - mu2)**2)/(2*torch.exp(logvar2)) - 1/2
        kld = torch.sum(kld) / bsize
        return pred2, kld

    def intype(self, t):
        if t == 'gpu':
            self.cuda()
        elif t == 'cpu':
            self.cpu()







class PolicyCNN(nn.Module):
    def __init__(self, opt):
        super(PolicyCNN, self).__init__()
        self.opt = opt
        self.encoder = policy_encoder(opt)
        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width
        self.fc = nn.Sequential(
            nn.Linear(self.hsize + opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, opt.npred*opt.n_actions)
        )

    def forward(self, state_images, states, actions):
        bsize = state_images.size(0)
        h = self.encoder(state_images, states)
        a = self.fc(h)
        a = a.view(bsize, self.opt.npred, self.opt.n_actions)
        return a, Variable(torch.zeros(1))


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
        elif t == 'cpu':
            self.cpu()











class PolicyAE(nn.Module):
    def __init__(self, opt, mfile):
        super(PolicyAE, self).__init__()
        self.opt = opt
        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width
        self.p_z = []

        if mfile == '':
            self.encoder = policy_encoder(opt)
            self.fc = nn.Sequential(
                nn.Linear(self.hsize + opt.n_hidden, opt.n_hidden),
#                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden),
#                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.npred*opt.n_actions)
            )

            self.a_encoder = nn.Sequential(
                nn.Linear(self.opt.npred*self.opt.n_actions, opt.n_hidden),
#                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden),
#                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden)
            )

        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            pretrained_model = torch.load(mfile)
            self.encoder1 = pretrained_model.encoder
            self.fc1 = pretrained_model.fc
            self.encoder2 = copy.deepcopy(pretrained_model.encoder)
            self.fc2 = copy.deepcopy(pretrained_model.fc)


        self.z_network = nn.Sequential(
            nn.BatchNorm1d(self.hsize + 2*opt.n_hidden),
            nn.Linear(self.hsize + 2*opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.nz)
            )

        self.z_expander = nn.Sequential(
            nn.Linear(opt.nz, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, self.hsize + opt.n_hidden)
        )


    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z)
        if self.use_cuda: z = z.cuda()
        return Variable(z)


    def forward(self, state_images, states, actions, save_z=False):
        bsize = state_images.size(0)
        h = self.encoder(state_images, states)
        if actions is not None:
            ha = self.a_encoder(actions.view(bsize, -1))
            z = self.z_network(torch.cat((h, ha), 1).view(bsize, -1))
            z_exp = self.z_expander(z)
            if save_z:
                self.save_z(z)
        else:
            z = self.sample_z(bsize)
            z_exp = self.z_expander(z)

        h = h + z_exp
        a = self.fc(h)
        a = a.view(bsize, self.opt.npred, self.opt.n_actions)
        return a, Variable(torch.zeros(1))


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False

















class PolicyEEN(nn.Module):
    def __init__(self, opt, mfile):
        super(PolicyEEN, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = policy_encoder(opt)
            self.fc = nn.Sequential(
                nn.Linear(self.hsize + opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.npred*opt.n_actions)
            )
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            pretrained_model = torch.load(mfile)
            self.encoder1 = pretrained_model.encoder
            self.fc1 = pretrained_model.fc
            self.encoder2 = copy.deepcopy(pretrained_model.encoder)
            self.fc2 = copy.deepcopy(pretrained_model.fc)

        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width

        self.z_network = nn.Sequential(
            nn.BatchNorm1d(opt.n_actions*opt.npred),
            nn.Linear(opt.n_actions*opt.npred, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.nz)
            )

        self.z_expander = nn.Sequential(
            nn.Linear(opt.nz, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, self.hsize + opt.n_hidden)
        )


    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z)
        if self.use_cuda: z = z.cuda()
        return Variable(z)


    def forward(self, state_images, states, actions, save_z=False):
        bsize = state_images.size(0)
        if actions is not None:
            h1 = self.encoder1(state_images, states)
            a1 = self.fc1(h1)
            a1 = a1.view(bsize, self.opt.npred, self.opt.n_actions)
            error = actions - a1
            error = Variable(error.data)
            z = self.z_network(error.view(bsize, -1))
            z_exp = self.z_expander(z)
            if save_z:
                self.save_z(z)
        else:
            z = self.sample_z(bsize)
            z_exp = self.z_expander(z)

        h2 = self.encoder2(state_images, states)
        h2 = h2 + z_exp
        a2 = self.fc2(h2)
        a2 = a2.view(bsize, self.opt.npred, self.opt.n_actions)
        return a2, Variable(torch.zeros(1))


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False











class PolicyEEN_LP(nn.Module):
    def __init__(self, opt, mfile):
        super(PolicyEEN, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = policy_encoder(opt)
            self.fc = nn.Sequential(
                nn.Linear(self.hsize + opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.npred*opt.n_actions)
            )
        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            pretrained_model = torch.load(mfile)
            self.encoder1 = pretrained_model.encoder
            self.fc1 = pretrained_model.fc
            self.encoder2 = copy.deepcopy(pretrained_model.encoder)
            self.fc2 = copy.deepcopy(pretrained_model.fc)

        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width

        self.z_network = nn.Sequential(
            nn.BatchNorm1d(opt.n_actions*opt.npred),
            nn.Linear(opt.n_actions*opt.npred, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.nz)
            )

        self.z_expander = nn.Sequential(
            nn.Linear(opt.nz, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, self.hsize + opt.n_hidden)
        )


    def save_z(self, z):
        if len(self.p_z) == 0:
            self.p_z = z.data.cpu()
        else:
            self.p_z = torch.cat((self.p_z, z.data.cpu()), 0)

    def sample_z(self, bsize):
        z = []
        for b in range(bsize):
            z.append(random.choice(self.p_z))
        z = torch.stack(z)
        if self.use_cuda: z = z.cuda()
        return Variable(z)


    def forward(self, state_images, states, actions, save_z=False):
        bsize = state_images.size(0)
        if actions is not None:
            h1 = self.encoder1(state_images, states)
            a1 = self.fc1(h1)
            a1 = a1.view(bsize, self.opt.npred, self.opt.n_actions)
            error = actions - a1
            error = Variable(error.data)
            z = self.z_network(error.view(bsize, -1))
            z_exp = self.z_expander(z)
            if save_z:
                self.save_z(z)
        else:
            z = self.sample_z(bsize)
            z_exp = self.z_expander(z)

        h2 = self.encoder2(state_images, states)
        h2 = h2 + z_exp
        a2 = self.fc2(h2)
        a2 = a2.view(bsize, self.opt.npred, self.opt.n_actions)
        return a2, Variable(torch.zeros(1))


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
            self.use_cuda = True
        elif t == 'cpu':
            self.cpu()
            self.use_cuda = False












class PolicyCNN_VAE(nn.Module):
    def __init__(self, opt, mfile=''):
        super(PolicyCNN_VAE, self).__init__()
        self.opt = opt

        if mfile == '':
            self.encoder = policy_encoder(opt)
            self.fc = nn.Sequential(
                nn.Linear(self.hsize + opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.n_hidden),
                nn.BatchNorm1d(opt.n_hidden),
                nn.ReLU(),
                nn.Linear(opt.n_hidden, opt.npred*opt.n_actions)
            )

        else:
            print(f'[initializing encoder and decoder with: {mfile}]')
            pretrained_model = torch.load(mfile)
            self.encoder = pretrained_model.encoder
            self.fc = pretrained_model.fc

        self.hsize = opt.nfeature*self.opt.h_height*self.opt.h_width


        self.z_network = nn.Sequential(
            nn.BatchNorm1d(opt.n_actions*opt.npred + self.hsize + opt.n_hidden),
            nn.Linear(opt.n_actions*opt.npred + self.hsize + opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, opt.n_hidden),
            nn.ReLU(),
            nn.BatchNorm1d(opt.n_hidden),
            nn.Linear(opt.n_hidden, 2*opt.nz)
            )

        self.z_expander = nn.Sequential(
            nn.Linear(opt.nz, opt.n_hidden),
            nn.ReLU(),
            nn.Linear(opt.n_hidden, self.hsize + opt.n_hidden)
        )


    def reparameterize(self, mu, logvar):
        if self.training:
            std = logvar.mul(0.5).exp_()
            eps = Variable(std.data.new(std.size()).normal_())
            return eps.mul(std).add_(mu)
        else:
            return mu

    def encode(self, a):
        bsize = a.size(0)
        z_params = self.z_network(a.view(bsize, -1)).view(bsize, self.opt.nz, 2)
        mu = z_params[:, :, 0]
        logvar = z_params[:, :, 1]
        return mu, logvar


    def forward(self, state_images, states, actions):
        bsize = state_images.size(0)
        state_images = state_images.view(bsize, 3*self.opt.ncond, 97, 20)
        states = states.view(bsize, -1)
        actions = actions.view(bsize, -1)

        h = self.encoder(state_images, states)

        mu, logvar = self.encode(torch.cat((h, actions), 1))
        z = self.reparameterize(mu, logvar)

        h = h + self.z_expander(z)
        a = self.fc(h)
        a = a.view(bsize, self.opt.npred, self.opt.n_actions)
        kld = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
        kld /= bsize
        return a, kld


    def intype(self, t):
        if t == 'gpu':
            self.cuda()
        elif t == 'cpu':
            self.cpu()



