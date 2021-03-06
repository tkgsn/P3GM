import sys
import torch
import math
import pathlib
filedir = pathlib.Path(__file__).resolve().parent
sys.path.append(str(filedir.parent / "privacy"))
import numpy as np
from tensorflow_privacy.privacy.analysis.rdp_accountant import compute_rdp, get_privacy_spent

sys.path.append(str(filedir.parent))
import dp_utils
import vae

# The method to compute the approximate KL divergence between Gaussian and MoG.
# MoG = [pi, mu, var]
def _p3gm_kl_divergence(gmm1, gmm2):
    mu1, var1 = gmm1[0], gmm1[1]
    pr_klds = []
    for i, (pi2, mu2, var2) in enumerate(zip(*gmm2)):
        kld = _kl_divergence((mu1, var1),(mu2, var2))
        pr_kld = -kld + torch.log(pi2)
        pr_klds.append(pr_kld)
    pr_klds = torch.stack(pr_klds)
    max_values = torch.max(pr_klds, dim=0)[0].reshape(1,-1)
    pr_klds = pr_klds - max_values
    var_kld = - max_values - torch.log(torch.sum(torch.exp(pr_klds), dim=0))
    return var_kld.reshape(-1)

# The method to compute the KL divergence between two Gaussians.
# gauss = [mu, var]
def _kl_divergence(gauss1, gauss2):
    mu1, var1 = gauss1[0], gauss1[1]
    mu2, var2 = gauss2[0].reshape(1,-1), gauss2[1].reshape(1,-1)
    
    dim = mu1.shape[1]
    
    log_var2 = torch.log(var2)
    log_var1 = torch.log(var1)
    
    term1 = torch.sum(log_var2, dim=1) - torch.sum(log_var1, dim=1)
    term2 = torch.mm(1/var2, torch.t(var1))[0]
    term3 = torch.diag(torch.mm((mu2 - mu1) * (1/var2), torch.t(mu2 - mu1)))
    
    return (1/2) * (term1 - dim + term2 + term3)

# The method to compute the sum of the privacy budget for P3GM.
# This method is depending on the tensorflow.privacy library
def analysis_privacy(lot_size, data_size, sgd_sigma, gmm_sigma, gmm_iter, gmm_n_comp, sgd_epoch, pca_sigma, delta):
    q = lot_size / data_size
    sgd_steps = int(math.ceil(sgd_epoch * data_size / lot_size))
    gmm_steps = gmm_iter * (2 * gmm_n_comp + 1)
    orders = ([1.25, 1.5, 1.75, 2., 2.25, 2.5, 3., 3.5, 4., 4.5] +
            list(range(5, 64)) + [128, 256, 512])
    pca_rdp = compute_rdp(1, pca_sigma, 1, orders)
    if sgd_steps == 0:
        sgd_rdp = np.array([0]*len(orders))
    else:
        sgd_rdp = compute_rdp(q, sgd_sigma, sgd_steps, orders)
    gmm_rdp = compute_rdp(1, gmm_sigma, gmm_steps, orders)

    rdp = pca_rdp + gmm_rdp + sgd_rdp
    
    eps, _, opt_order = get_privacy_spent(orders, rdp, target_delta=delta)
    
    index = orders.index(opt_order)
    print(f"ratio(pca:gmm:sgd):{pca_rdp[index]/rdp[index]}:{gmm_rdp[index]/rdp[index]}:{sgd_rdp[index]/rdp[index]}")
    print(f"GMM + SGD + PCA (MA): {eps}, {delta}-DP")
    
    return eps, [pca_rdp[index]/rdp[index], gmm_rdp[index]/rdp[index], sgd_rdp[index]/rdp[index]]

# The P3GM class which inherets a VAE
class P3GM(vae.VAE):

    # The staticmethod to compute the sum of the privacy budget (This refers to the analysis_privacy method)
    @staticmethod
    def cp_epsilon(data_size, lot_size, pca_sigma, gmm_sigma, gmm_iter, gmm_n_comp, sgd_sigma, sgd_epoch, delta):
        return analysis_privacy(lot_size, data_size, sgd_sigma, gmm_sigma, gmm_iter, gmm_n_comp, sgd_epoch, pca_sigma, delta=delta)[0]

    # The initilization method to construct networks.
    # z_dim is the number of components for PCA (the dimensionality of z)
    # latent_din is the number of nodes for hidden layers
    def __init__(self, dims, device, z_dim=10, latent_dim=1000):
        if z_dim > sum(dims):
            z_dim = sum(dims)
        super().__init__(dims, device, z_dim=z_dim, latent_dim=latent_dim)

    # The method to train P3GM.
    # 1. fit PCA 2. fit GMM, 3. train neural networks
    def train(self, train_loader, random_state, **kwargs):
        
        # hyperparameters
        pca_sigma = kwargs.get("pca_sigma")
        gmm_sigma = kwargs.get("gmm_sigma")
        gmm_n_comp = kwargs.get("gmm_n_comp")
        gmm_iter = kwargs.get("gmm_iter")
        sgd_epoch = kwargs.get("sgd_epoch")
        sgd_sigma = kwargs.get("sgd_sigma")
        clipping = kwargs.get("clipping")
        num_microbatches = kwargs.get("num_microbatches")
        lr = kwargs.get("lr")
        nopretrain = kwargs.get("nopretrain")

        self.sgd_sigma, self.clipping, self.num_microbatches = sgd_sigma, clipping, num_microbatches

        data = train_loader.get_data()
        
        # train PCA
        self._train_pca(data, pca_sigma, self.z_dim, random_state)
        feature = self.pca.transform(data)

        # train GMM
        self._train_gmm(feature, gmm_sigma, gmm_n_comp, gmm_iter, random_state)
        self._set_parameters()

        if not nopretrain:
            self.pcapretrain()

        # train neural networks
        super().train(train_loader, **kwargs)

    def _inverse_pca(self, X):
        return torch.tensor(self.pca.inverse_transform(X.cpu()))

    def _transform_pca(self, X):
        X = X - self.pca_mean_
        X_transformed = torch.mm(X, self.pca_components_)
        return X_transformed

    def encode(self, x):
        h1 = torch.nn.functional.relu(self.fc1(x))
        return self._transform_pca(x), self.fc22(h1)

    def generate_data_by_pca(self, data_size):
        z = self.z_sample(data_size)
        data = self._inverse_pca(z)
        return z.detach().cpu().numpy(), data.detach().cpu().numpy()

    def set_vae(self):
        self.encode = super().encode
        self.train = super().train
        self.loss_function = super().loss_function
        

    def _set_parameters(self):
        self.gmm_weights_ = torch.nn.Parameter(torch.tensor(self.gmm.weights_, dtype=torch.float32), requires_grad=False).to(self.device)
        self.gmm_means_ = torch.nn.Parameter(torch.tensor(self.gmm.means_, dtype=torch.float32), requires_grad=False).to(self.device)
        self.gmm_covariances_ = torch.nn.Parameter(torch.tensor(self.gmm.covariances_, dtype=torch.float32), requires_grad=False).to(self.device)
        self.gmm_params = [self.gmm_weights_, self.gmm_means_, self.gmm_covariances_]
        self.pca_components_ = torch.nn.Parameter(torch.t(torch.tensor(self.pca.components_[:self.pca.n_components], dtype=torch.float32)), requires_grad=False).to(self.device)
        self.pca_mean_ = torch.nn.Parameter(torch.tensor(self.pca.mean_, dtype=torch.float32), requires_grad=False).to(self.device)

    def _train_pca(self, data, sigma, n_comp, random_state):
        self.pca = dp_utils.dp_pca.DP_GAUSSIAN_PCA(sigma=sigma, n_components=n_comp, random_state=random_state)
        self.pca.fit(data)

    def _train_gmm(self, feature, gmm_sigma, gmm_n_comp, gmm_iter, random_state):
        self.gmm = dp_utils.dp_gaussian_mixture.DPGaussianMixture(sigma=gmm_sigma, n_components=gmm_n_comp, n_iter=gmm_iter, random_state=random_state)
        self.gmm.fit(feature)


    def loss_function(self, data, means, logvar):
        
        z = self.reparameterize(means, logvar)
        recon_x = self.decode(z)
        SE = torch.sum((recon_x - data) ** 2, dim=1)
        KLD = _p3gm_kl_divergence([means, torch.exp(logvar)], self.gmm_params)

        return SE, KLD

    def loss_function_ae(self, data, means, logvar):
        
        z = self.reparameterize(means, logvar)
        recon_x = self.decode(z)
        SE = torch.sum((recon_x - data) ** 2, dim=1)
        KLD = torch.zeros(SE.shape).to(self.device)

        return SE, KLD

    def pcapretrain(self):

        print("Initializing the networks...")
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)

        for _ in range(3000):
            z, data = self.generate_data_by_pca(100)

            z = torch.tensor(z, dtype=torch.float32).to(self.device)
            data = torch.tensor(data, dtype=torch.float32).to(self.device)

            recon_data = self.decode(z)
            losses = torch.sum((recon_data - data) ** 2, dim=1)
            optimizer.zero_grad()
            self.backward(losses, no_dp=True)
            optimizer.step()


    # The method to backward the neural networks
    # P3GM uses noised step for DP-SGD
    def backward(self, losses, **kwargs):
        no_dp = kwargs.get('no_dp', False)
        if not no_dp:
            self._noised_step(losses, self.sgd_sigma, self.clipping, self.num_microbatches)
        else:
            super().backward(losses)

    # The method for the implementation of DP-SGD
    def _noised_step(self, losses, sigma, clipping, num_microbatches=3):

        saved_var = dict()
        parameters = list(filter(lambda p: p.requires_grad, self.parameters()))
        named_parameters = list(filter(lambda p: p[1].requires_grad, self.named_parameters()))

        for tensor_name, tensor in named_parameters:
            saved_var[tensor_name] = torch.zeros_like(tensor)

        n_batch = len(losses)
        n_loss_in_microbatch = math.ceil(n_batch / num_microbatches)

        for i in range(num_microbatches):
            loss = losses[i*n_loss_in_microbatch:(i+1)*n_loss_in_microbatch].mean()
            loss.backward(retain_graph=True)

            torch.nn.utils.clip_grad_norm_(parameters, clipping)
            for tensor_name, tensor in named_parameters:
                new_grad = tensor.grad
                if tensor.grad is None:
                    continue
                saved_var[tensor_name].add_(new_grad)
            self.zero_grad()

        for tensor_name, tensor in named_parameters:
            if tensor.grad is None:
                continue
            noise = torch.FloatTensor(tensor.grad.shape).normal_(0, sigma * clipping).to(self.device)
            saved_var[tensor_name].add_(noise)
            tensor.grad = saved_var[tensor_name] / num_microbatches