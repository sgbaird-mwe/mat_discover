"""Materials discovery using Earth Mover's Distance, DensMAP embeddings, and HDBSCAN*.

- create distance matrix
- apply densMAP
- create clusters via HDBSCAN*
- search for interesting materials, for example:
     - high-target/low-density
     - materials with high-target surrounded by materials with low targets
"""
# saving class objects: https://stackoverflow.com/a/37076668/13697228
import dill as pickle
from pathlib import Path
from os.path import join

# retrieve static file from package: https://stackoverflow.com/a/20885799/13697228
from importlib.resources import open_text

from warnings import warn
from operator import attrgetter
from chem_wasserstein.utils.Timer import Timer, NoTimer

import matplotlib.pyplot as plt

# from ml_matrics import density_scatter

import numpy as np
import pandas as pd
from scipy.stats import multivariate_normal, wasserstein_distance
from sklearn.preprocessing import MinMaxScaler, StandardScaler, RobustScaler
from sklearn.model_selection import LeaveOneGroupOut, train_test_split
from sklearn.manifold import MDS
from sklearn.metrics import mean_squared_error

# from sklearn.decomposition import PCA

import umap
import hdbscan

from chem_wasserstein.ElM2D_ import ElM2D

from mat_discover.utils.nearest_neigh import nearest_neigh_props
from mat_discover.utils.pareto import pareto_plot  # , get_pareto_ind
from mat_discover.utils.plotting import (
    umap_cluster_scatter,
    cluster_count_hist,
    target_scatter,
    dens_scatter,
    dens_targ_scatter,
)

from crabnet.train_crabnet import get_model

plt.rcParams.update(
    {
        "figure.autolayout": True,
        "figure.figsize": [3.5, 3.5],
        "figure.dpi": 300,
        "xtick.direction": "in",
    }
)


def my_mvn(mu_x, mu_y, r):
    """Calculate multivariate normal at (mu_x, mu_y) with constant radius, r."""
    return multivariate_normal([mu_x, mu_y], [[r, 0], [0, r]])


def groupby_formula(df, how="max"):
    """Group identical compositions together and preserve original indices.

    See https://stackoverflow.com/a/49216427/13697228

    Parameters
    ----------
    df : DataFrame
        At minimum should contain "formula" and "target" columns.
    how : str, optional
        How to perform the "groupby", either "mean" or "max", by default "max"

    Returns
    -------
    DataFrame
        The grouped DataFrame such that the original indices are preserved.
    """
    grp_df = (
        df.reset_index()
        .groupby(by="formula")
        .agg({"index": lambda x: tuple(x), "target": "max"})
        .reset_index()
    )
    return grp_df


class Discover:
    """
    A Materials Discovery class.

    Uses chemical-based distances, dimensionality reduction, clustering,
    and plotting to search for high performing, chemically unique compounds
    relative to training data.
    """

    def __init__(
        self,
        timed: bool = True,
        dens_lambda: float = 1.0,
        plotting: bool = False,
        pdf: bool = True,
        n_neighbors: int = 10,
        verbose: bool = True,
        mat_prop_name="test-property",
        dummy_run=False,
        Scaler=RobustScaler,
        figure_dir="figures",
        table_dir="tables",
        groupby_filter="max",
        pred_weight=1,
        device="cuda",
        dist_device=None,
    ):
        """Initialize a Discover() class.

        Parameters
        ----------
        timed : bool, optional
            Whether or not timing is reported, by default True

        dens_lambda : float, optional
            "Controls the regularization weight of the density correlation term in
            densMAP. Higher values prioritize density preservation over the UMAP
            objective, and vice versa for values closer to zero. Setting this parameter
            to zero is equivalent to running the original UMAP algorithm." Source:
            https://umap-learn.readthedocs.io/en/latest/api.html, by default 1.0

        plotting : bool, optional
            Whether to create and save various compound-wise and cluster-wise figures,
            by default False

        pdf : bool, optional
            Whether or not probability density function values are computed, by default
            True

        n_neighbors : int, optional
            Number of neighbors to consider when computing k_neigh_avg (i.e. peak
            proxy), by default 10

        verbose : bool, optional
            Whether to print verbose information, by default True

        mat_prop_name : str, optional
            A name that helps identify the training target property, by default
            "test-property"

        dummy_run : bool, optional
            Whether to use MDS instead of UMAP to run quickly for small datasets. Note
            that MDS takes longer for UMAP for large datasets, by default False

        Scaler : str or class, optional
            Scaler to use for weighted_score (i.e. weighted score of target and proxy
            values) Target and proxy are separately scaled using Scaler before taking
            the weighted sum. Possible values are "MinMaxScaler", "StandardScaler",
            "RobustScaler", or an sklearn.preprocessing scaler class, by default RobustScaler.

        figure_dir, table_dir : str, optional
            Relative or absolute path to directory at which to save figures or tables,
            by default "figures" and "tables", respectively. The directory will be
            created if it does not exist already.

        pred_weight : int, optional
            Weighting applied to the predicted target values, by default 1 (i.e. equal
            weighting between predictions and proxies). For example, to weight the
            predicted targets at twice that of the proxy values, set to 2.

        device : str, optional
            Which device to perform the computation on. Possible values are "cpu" and
            "cuda", by default "cuda".

        dist_device : str, optional
            Which device to perform the computation on for the distance computations
            specifically. Possible values are "cpu", "cuda", and None, by default None.
            If None, default to `device`.
        """
        if timed:
            self.Timer = Timer
        else:
            self.Timer = NoTimer

        self.dens_lambda = dens_lambda
        self.plotting = plotting
        self.pdf = pdf
        self.n_neighbors = n_neighbors
        self.verbose = verbose
        self.mat_prop_name = mat_prop_name
        self.dummy_run = dummy_run
        self.figure_dir = figure_dir
        self.table_dir = table_dir
        self.pred_weight = pred_weight
        self.device = device
        if dist_device is None:
            self.dist_device = self.device
        else:
            self.dist_device = dist_device

        if type(Scaler) is str:
            scalers = {
                "MinMaxScaler": MinMaxScaler,
                "StandardScaler": StandardScaler,
                "RobustScaler": RobustScaler,
            }
            self.Scaler = scalers[Scaler]
        else:
            self.Scaler = Scaler

        if self.device == "cpu":
            self.force_cpu = True
        else:
            self.force_cpu = False

        self.mapper = ElM2D(target=self.dist_device)  # type: ignore
        self.dm = None
        # self.formula = None
        # self.target = None
        self.train_formula = None
        self.train_target = None
        self.train_df = None
        self.val_df = None
        self.true_avg_targ = None
        self.pred_avg_targ = None
        self.train_avg_targ = None

        # create dir https://stackoverflow.com/a/273227/13697228
        Path(self.figure_dir).mkdir(parents=True, exist_ok=True)
        Path(self.table_dir).mkdir(parents=True, exist_ok=True)

    def fit(self, train_df):
        """Fit CrabNet model to training data.

        Parameters
        ----------
        train_df : DataFrame
            Should contain "formula" and "target" columns.
        """
        # unpack
        self.train_df = train_df
        self.train_formula = train_df["formula"]
        self.train_target = train_df["target"]

        # TODO: remove the "val MAE", which is wrong (should be NaN or just not displayed)
        # turns out this is a bit more difficult because of use of self.data_loader
        with Timer("train-CrabNet"):
            self.crabnet_model = get_model(
                mat_prop=self.mat_prop_name,
                train_df=train_df,
                learningcurve=False,
                force_cpu=self.force_cpu,
            )

        # TODO: UMAP on new data (i.e. add metric != "precomputed" functionality)

    def predict(
        self,
        val_df,
        plotting=None,
        umap_random_state=None,
        pred_weight=None,
        dummy_run=None,
    ):
        """Predict target and proxy for validation dataset.

        Parameters
        ----------
        val_df : DataFrame
            Validation dataset containing "formula" and "target" (populate with 0's if
            not available).
        plotting : bool, optional
            Whether to plot, by default None
        umap_random_state : int or None, optional
            The random seed to use for UMAP, by default None
        pred_weight : int, optional
            The weight to assign to the predictions (proxy_weight is 1 by default), by default None.

            If neither pred_weight nor self.pred_weight is specified, it defaults to 1.
            When specified, pred_weight takes precedence over self.pred_weight.
        dummy_run : bool, optional
            Whether to use MDS in place of the (typically more expensive) DensMAP, by default None.

            If neither dummy_run nor self.dummy_run is specified, it defaults to (effectively) being False. When specified, dummy_run takes precedence over self.dummy_run.

        Returns
        -------
        dens_score, peak_score
            Scaled discovery scores for density and peak proxies.
        """
        self.val_df = val_df

        # CrabNet
        # TODO: parity plot
        crabnet_model = self.crabnet_model
        # CrabNet predict output format: (act, pred, formulae, uncert)
        self.train_true, train_pred, _, self.train_sigma = crabnet_model.predict(
            self.train_df
        )
        self.val_true, self.val_pred, _, self.val_sigma = crabnet_model.predict(
            self.val_df
        )
        pred = np.concatenate((train_pred, self.val_pred), axis=0)

        self.val_rmse = mean_squared_error(self.val_true, self.val_pred, squared=False)
        if self.verbose:
            print("val RMSE: ", self.val_rmse)

        train_formula = self.train_df["formula"]
        train_target = self.train_df["target"]
        self.val_formula = self.val_df["formula"]
        if "target" in self.val_df.columns:
            val_target = self.val_df["target"]
        else:
            val_target = self.val_pred

        self.all_formula = pd.concat((train_formula, self.val_formula), axis=0)
        self.all_target = pd.concat((train_target, val_target), axis=0)

        ntrain, nval = len(train_formula), len(self.val_formula)
        ntot = ntrain + nval
        train_ids, val_ids = np.arange(ntrain), np.arange(ntrain, ntot)

        # distance matrix
        with self.Timer("fit-wasserstein"):
            self.mapper.fit(self.all_formula)
            self.dm = self.mapper.dm

        # TODO: look into UMAP via GPU
        # TODO: create and use fast, built-in Wasserstein UMAP method

        # UMAP (clustering and visualization) (or MDS for a quick run)
        if (dummy_run is None and self.dummy_run) or dummy_run:
            umap_trans = MDS(n_components=2, dissimilarity="precomputed").fit(self.dm)
            std_trans = umap_trans
            self.umap_emb = umap_trans.embedding_
            self.std_emb = umap_trans.embedding_
            self.umap_r_orig = np.random.rand(self.dm.shape[0])
            self.std_r_orig = np.random.rand(self.dm.shape[0])
        else:
            umap_trans = self.umap_fit_cluster(self.dm, random_state=umap_random_state)
            std_trans = self.umap_fit_vis(self.dm, random_state=umap_random_state)
            self.umap_emb, self.umap_r_orig = self.extract_emb_rad(umap_trans)[:2]
            self.std_emb, self.std_r_orig = self.extract_emb_rad(std_trans)[:2]

        # HDBSCAN*
        if (dummy_run is None and self.dummy_run) or dummy_run:
            min_cluster_size = 5
            min_samples = 1
        else:
            min_cluster_size = 50
            min_samples = 1
        clusterer = self.cluster(
            self.umap_emb, min_cluster_size=min_cluster_size, min_samples=min_samples
        )
        self.labels = self.extract_labels_probs(clusterer)[0]

        # np.unique while preserving order https://stackoverflow.com/a/12926989/13697228
        lbl_ids = np.unique(self.labels, return_index=True)[1]
        self.unique_labels = [self.labels[lbl_id] for lbl_id in sorted(lbl_ids)]

        self.val_labels = self.labels[val_ids]

        # Probability Density Function Summation
        if self.pdf:
            self.pdf_x, self.pdf_y, self.pdf_sum = self.mvn_prob_sum(
                self.std_emb, self.std_r_orig
            )

        # validation density contributed by training densities
        train_emb = self.umap_emb[:ntrain]
        train_r_orig = self.umap_r_orig[:ntrain]
        val_emb = self.umap_emb[ntrain:]

        with self.Timer("train-val-pdf-summation"):
            mvn_list = list(map(my_mvn, train_emb[:, 0], train_emb[:, 1], train_r_orig))
            pdf_list = [mvn.pdf(val_emb) for mvn in mvn_list]
            self.val_dens = np.sum(pdf_list, axis=0)
            self.val_log_dens = np.log(self.val_dens)

        # cluster-wise predicted average
        cluster_pred = np.array(
            [pred.ravel()[self.labels == lbl] for lbl in self.unique_labels],
            dtype=object,
        )
        self.cluster_avg = np.vectorize(np.mean)(cluster_pred).reshape(-1, 1)

        # cluster-wise validation fraction
        train_ct, val_ct = np.array(
            [
                [
                    np.count_nonzero(self.labels[ids] == lbl)
                    for lbl in self.unique_labels
                ]
                for ids in [train_ids, val_ids]
            ]
        )
        self.val_frac = (val_ct / (train_ct + val_ct)).reshape(-1, 1)

        # # Scale and weight the cluster data
        # # REVIEW: Which Scaler to use? RobustScaler means no set limits on "score"
        self.cluster_score = self.weighted_score(self.cluster_avg, self.val_frac)

        # compound-wise scores (i.e. individual compounds)
        self.rad_neigh_avg_targ, self.k_neigh_avg_targ = nearest_neigh_props(
            self.dm, pred
        )
        self.val_rad_neigh_avg = self.rad_neigh_avg_targ[val_ids]
        self.val_k_neigh_avg = self.k_neigh_avg_targ[val_ids]

        self.rad_score = self.weighted_score(
            self.val_pred, self.val_rad_neigh_avg, pred_weight=pred_weight
        )
        self.peak_score = self.weighted_score(
            self.val_pred, self.val_k_neigh_avg, pred_weight=pred_weight
        )
        self.dens_score = self.weighted_score(
            self.val_pred, self.val_dens, pred_weight=pred_weight
        )

        # Plotting
        if (self.plotting and plotting is None) or plotting:
            self.plot()

        self.rad_score_df = self.sort(self.rad_score)
        self.peak_score_df = self.sort(self.peak_score)
        self.dens_score_df = self.sort(self.dens_score)

        # create dir https://stackoverflow.com/a/273227/13697228
        Path(self.table_dir).mkdir(parents=True, exist_ok=True)

        self.merge()

        return self.dens_score, self.peak_score

    def weighted_score(self, pred, proxy, pred_weight=None):
        """Calculate weighted discovery score using the predicted target and proxy.

        Parameters
        ----------
        pred : 1D Array
            Predicted target property values.
        proxy : 1D Array
            Predicted proxy values (e.g. density or peak proxies).
        pred_weight : int, optional
            The weight to assign to the predictions (proxy_weight is 1 by default), by default 2

        Returns
        -------
        1D array
            Discovery scores.
        """
        if self.pred_weight is not None and pred_weight is None:
            pred_weight = self.pred_weight
        elif self.pred_weight is None and pred_weight is None:
            pred_weight = 1

        pred = pred.ravel().reshape(-1, 1)
        proxy = proxy.ravel().reshape(-1, 1)
        # Scale and weight the cluster data
        pred_scaler = self.Scaler().fit(pred)
        pred_scaled = pred_weight * pred_scaler.transform(pred)
        proxy_scaler = self.Scaler().fit(-1 * proxy)
        proxy_scaled = proxy_scaler.transform(-1 * proxy)

        # combined cluster data
        comb_data = pred_scaled + proxy_scaled
        comb_scaler = self.Scaler().fit(comb_data)

        # cluster scores range between 0 and 1
        score = comb_scaler.transform(comb_data).ravel()
        return score

    def sort(self, score):
        """Sort (rank) compounds by their proxy score.

        Parameters
        ----------
        score : 1D Array
            Discovery scores.

        Returns
        -------
        DataFrame
            Contains "formula", "prediction", "density", and "score".
        """
        score_df = pd.DataFrame(
            {
                "formula": self.val_formula,
                "prediction": self.val_pred,
                "density": self.val_dens,
                "score": score,
            }
        )
        score_df.sort_values("score", ascending=False, inplace=True)
        return score_df

    def merge(self):
        """Perform an outer merge of the density and peak proxy rankings.

        Returns
        -------
        DataFrame
            Outer merge of the two proxy rankings.
        """
        self.dens_score_df.rename(columns={"score": "Density Score"}, inplace=True)
        self.peak_score_df.rename(columns={"score": "Peak Score"}, inplace=True)

        comb_score_df = self.dens_score_df[["formula", "Density Score"]].merge(
            self.peak_score_df[["formula", "Peak Score"]], how="outer"
        )

        comb_formula = self.dens_score_df.head(10)[["formula"]].merge(
            self.peak_score_df.head(10)[["formula"]], how="outer"
        )

        self.comb_out_df = comb_score_df[np.isin(comb_score_df.formula, comb_formula)]

        self.dens_score_df.head(10).to_csv(
            join(self.table_dir, "dens-score.csv"), index=False, float_format="%.3f"
        )
        self.peak_score_df.head(10).to_csv(
            join(self.table_dir, "peak-score.csv"), index=False, float_format="%.3f"
        )
        self.comb_out_df.to_csv(
            join(self.table_dir, "comb-score.csv"), index=False, float_format="%.3f"
        )

        return self.comb_out_df

    def group_cross_val(self, df, umap_random_state=None, dummy_run=None):
        """Perform leave-one-cluster-out cross-validation (LOCO-CV).

        Parameters
        ----------
        df : DataFrame
            Contains "formula" and "target" (all the data)
        umap_random_state : int, optional
            Random state to use for DensMAP embedding, by default None
        dummy_run : bool, optional
            Whether to perform a "dummy run" (i.e. use multi-dimensional scaling which is faster), by default None

        Returns
        -------
        float
            Scaled, weighted error based on Wasserstein distance (i.e. a sorting distance).

        Raises
        ------
        ValueError
            Needs to have at least one cluster. It is assumed that there will always be a non-cluster
            (i.e. unclassified points) if there is only 1 cluster.

        Notes
        -----
        TODO: highest mean vs. highest single target value
        """
        # TODO: remind people in documentation to use a separate Discover() instance if they wish to access fit *and* gcv attributes
        self.all_formula = df["formula"]
        self.all_target = df["target"]

        # distance matrix
        with self.Timer("fit-wasserstein"):
            self.mapper.fit(self.all_formula)
            self.dm = self.mapper.dm

        # UMAP (clustering and visualization)
        if (dummy_run is None and self.dummy_run) or dummy_run:
            umap_trans = MDS(n_components=2, dissimilarity="precomputed").fit(self.dm)
            self.umap_emb = umap_trans.embedding_
            min_cluster_size = 5
        else:
            umap_trans = self.umap_fit_cluster(self.dm, random_state=umap_random_state)
            self.umap_emb = self.extract_emb_rad(umap_trans)[0]
            min_cluster_size = 50

        # HDBSCAN*
        clusterer = self.cluster(self.umap_emb, min_cluster_size=min_cluster_size)
        self.labels = self.extract_labels_probs(clusterer)[0]

        # Group Cross Validation Setup
        logo = LeaveOneGroupOut()

        self.n_clusters = np.max(self.labels)
        if self.n_clusters <= 0:
            raise ValueError("no clusters or only one cluster")
        n_test_clusters = np.max([1, int(self.n_clusters * 0.1)])

        """Difficult to repeatably set aside a test set for a given clustering result;
        by changing clustering parameters, the test clusters might change, so using
        seed=42 for UMAP random_state, then seed=10 for setting aside clusters.
        Note that "unclustered" is never assigned as a test_cluster, and so is always
        included in tv_cluster_ids (tv===train_validation)."""
        # np.random.default_rng(seed=10)
        # test_cluster_ids = np.random.choice(self.n_clusters + 1, n_test_clusters)
        # np.random.default_rng()

        # test_ids = np.isin(self.labels, test_cluster_ids)
        # tv_cluster_ids = np.setdiff1d(
        #     np.arange(-1, self.n_clusters + 1), test_cluster_ids
        # )
        # tv_ids = np.isin(self.labels, tv_cluster_ids)

        X, y = self.all_formula, self.all_target
        # X_test, y_test = X[test_ids], y[test_ids]  # noqa
        # X_tv, y_tv, labels_tv = X[tv_ids], y[tv_ids], self.labels[tv_ids]

        # Group Cross Validation
        # for train_index, val_index in logo.split(X_tv, y_tv, self.labels):
        # TODO: group cross-val parity plot
        if self.verbose:
            print("Number of iterations (i.e. clusters): ", self.n_clusters)

        avg_targ = [
            self.single_group_cross_val(X, y, train_index, val_index, i)
            for i, (train_index, val_index) in enumerate(logo.split(X, y, self.labels))
        ]
        out = np.array(avg_targ).T
        self.true_avg_targ, self.pred_avg_targ, self.train_avg_targ = out

        # np.unique while preserving order https://stackoverflow.com/a/12926989/13697228
        lbl_ids = np.unique(self.labels, return_index=True)[1]
        self.avg_labels = [self.labels[lbl_id] for lbl_id in sorted(lbl_ids)]

        # Sorting "Error" Setup
        u = np.flip(np.cumsum(np.linspace(0, 1, len(self.true_avg_targ))))
        v = u.copy()
        # sorting indices from high to low
        self.sorter = np.flip(self.true_avg_targ.argsort())

        # sort everything by same indices to preserve cluster order
        u_weights = self.true_avg_targ[self.sorter]
        v_weights = self.pred_avg_targ[self.sorter]
        dummy_v_weights = self.train_avg_targ[self.sorter]

        # Weighted distance between CDFs as proxy for "how well sorted" (i.e. sorting metric)
        self.error = wasserstein_distance(
            u, v, u_weights=u_weights, v_weights=v_weights
        )
        if self.verbose:
            print("Weighted group cross validation error =", self.error)

        # Dummy Error (i.e. if you "guess" the mean of training targets)
        self.dummy_error = wasserstein_distance(
            u,
            v,
            u_weights=u_weights,
            v_weights=dummy_v_weights,
        )

        # Similar to Matbench "scaled_error"
        self.scaled_error = self.error / self.dummy_error
        if self.verbose:
            print("Weighted group cross validation scaled error =", self.scaled_error)
        return self.scaled_error

    def single_group_cross_val(self, X, y, train_index, val_index, iter):
        """Perform leave-one-cluster-out cross-validation.

        Parameters
        ----------
        X : list of str
            Chemical formulae.
        y : 1d array of float
            Target properties.
        train_index, val_index : 1d array of int
            Training and validation indices for a given split, respectively.
        iter : int
            Iteration (i.e. how many clusters have been processed so far).

        Returns
        -------
        true_avg_targ, pred_avg_targ, train_avg_targ : 1d array of float
            True, predicted, and training average targets for each of the clusters.
            average target is used to create a "dummy" measure of performance (i.e. one
            of the the simplest "models" you can use, the average of the training data).
        """
        if self.verbose:
            print("Iteration: ", iter)

        Xn, yn = X.to_numpy(), y.to_numpy()
        X_train, X_val = Xn[train_index], Xn[val_index]
        y_train, y_val = yn[train_index], yn[val_index]

        train_df = pd.DataFrame({"formula": X_train, "target": y_train})
        val_df = pd.DataFrame({"formula": X_val, "target": y_val})
        test_df = None

        # test_df = pd.DataFrame({"formula": X_test, "property": y_test})

        self.crabnet_model = get_model(
            mat_prop=self.mat_prop_name,
            train_df=train_df,
            val_df=val_df,
            learningcurve=False,
            verbose=False,
            force_cpu=self.force_cpu,
        )

        # CrabNet predict output format: (act, pred, formulae, uncert)
        train_true, train_pred, _, train_sigma = self.crabnet_model.predict(train_df)
        val_true, val_pred, _, val_sigma = self.crabnet_model.predict(val_df)
        if test_df is not None:
            _, test_pred, _, test_sigma = self.crabnet_model.predict(test_df)

        true_avg_targ = np.mean(val_true)
        pred_avg_targ = np.mean(val_pred)

        train_avg_targ = np.mean(train_true)

        # nearest neighbors
        # rad_neigh_avg_targ, k_neigh_avg_targ = nearest_neigh_props(
        #     self.dm, self.all_target
        # )

        # pareto_ind = get_pareto_ind(k_neigh_avg_targ, target)

        # # TODO: determine pareto locations of validation cluster
        # non_pareto_ind = np.setdiff1d(range(len(target)), pareto_ind)
        # val_pareto_ind = np.intersect1d(pareto_ind, val_index)
        # if val_pareto_ind.size == 0:
        #     warn("No validation points on pareto front.")

        # val_neigh_avg_targ = k_neigh_avg_targ[val_index]

        # k_neigh_avg_targ = nearest_neigh_props(
        #     self.dm[:, val_index][val_index, :], pred_targ
        # )[1]

        # better_ind = pred_targ[
        #     pred_targ > 0.5 * np.max(pred_targ)
        #     and val_avg_targ < 0.5 * np.max(val_neigh_avg_targ)
        # ]

        # # log densMAP densities
        # log_dens = self.log_density()
        # val_dens = log_dens[val_index]

        # pkval_pareto_ind = get_pareto_ind(val_avg_targ, val_targ)
        # densval_pareto_ind = get_pareto_ind(val_dens, val_targ)

        return true_avg_targ, pred_avg_targ, train_avg_targ

    def cluster(self, umap_emb, min_cluster_size=50, min_samples=5):
        """Cluster using HDBSCAN*.

        Parameters
        ----------
        umap_emb : nD Array
            DensMAP embedding coordinates.
        min_cluster_size : int, optional
            "The minimum size of clusters; single linkage splits that contain fewer
            points than this will be considered points "falling out" of a cluster rather
            than a cluster splitting into two new clusters." (source: HDBSCAN* docs), by
            default 50
        min_samples : int, optional
            "The number of samples in a neighbourhood for a point to be considered a
            core point." (source: HDBSCAN* docs), by default 5

        Returns
        -------
        clusterer : HDBSCAN class
            HDBSCAN clusterer fitted to UMAP embeddings.
        """
        with self.Timer("HDBSCAN*"):
            clusterer = hdbscan.HDBSCAN(
                min_samples=min_samples,
                cluster_selection_epsilon=0.63,
                min_cluster_size=min_cluster_size,
                # allow_single_cluster=True,
            ).fit(umap_emb)
        return clusterer

    def extract_labels_probs(self, clusterer):
        """Extract cluster IDs (`labels`) and `probabilities` from HDBSCAN* `clusterer`.

        Parameters
        ----------
        clusterer : HDBSCAN class
            Instantiated HDBSCAN* class for clustering.

        Returns
        -------
        labels_ : ndarray, shape (n_samples, )
            "Cluster labels for each point in the dataset given to fit(). Noisy samples
            are given the label -1." (source: HDBSCAN* docs)

        probabilities_ : ndarray, shape (n_samples, )
            "The strength with which each sample is a member of its assigned cluster.
            Noise points have probability zero; points in clusters have values assigned
            proportional to the degree that they persist as part of the cluster." (source: HDBSCAN* docs)
        """
        labels, probabilities = attrgetter("labels_", "probabilities_")(clusterer)
        return labels, probabilities

    def umap_fit_cluster(self, dm, random_state=None):
        """Perform DensMAP fitting for clustering.

        See https://umap-learn.readthedocs.io/en/latest/clustering.html.

        Parameters
        ----------
        dm : ndarray
            Pairwise Element Mover's Distance (`ElMD`) matrix within a single set of
            points.

        random_state: int, RandomState instance or None, optional (default: None)
            "If int, random_state is the seed used by the random number generator; If
            RandomState instance, random_state is the random number generator; If None,
            the random number generator is the RandomState instance used by
            `np.random`." (source: UMAP docs)

        Returns
        -------
        umap_trans : UMAP class
            A UMAP class fitted to `dm`.

        See Also
        --------
        umap.UMAP : UMAP class.
        """
        with self.Timer("fit-UMAP"):
            umap_trans = umap.UMAP(
                densmap=True,
                output_dens=True,
                dens_lambda=self.dens_lambda,
                n_neighbors=30,
                min_dist=0,
                n_components=2,
                metric="precomputed",
                random_state=random_state,
            ).fit(dm)
        return umap_trans

    def umap_fit_vis(self, dm, random_state=None):
        """Perform DensMAP fitting for visualization.

        See https://umap-learn.readthedocs.io/en/latest/clustering.html.

        Parameters
        ----------
        dm : ndarray
            Pairwise Element Mover's Distance (`ElMD`) matrix within a single set of
            points.

        random_state: int, RandomState instance or None, optional (default: None)
            "If int, random_state is the seed used by the random number generator; If
            RandomState instance, random_state is the random number generator; If None,
            the random number generator is the RandomState instance used by
            `np.random`." (source: UMAP docs)

        Returns
        -------
        std_trans : UMAP class
            A UMAP class fitted to `dm`.

        See Also
        --------
        umap.UMAP : UMAP class.
        """
        with self.Timer("fit-vis-UMAP"):
            std_trans = umap.UMAP(
                densmap=True,
                output_dens=True,
                dens_lambda=self.dens_lambda,
                metric="precomputed",
                random_state=random_state,
            ).fit(dm)
        return std_trans

    def extract_emb_rad(self, trans):
        """Extract densMAP embedding and radii.

        Parameters
        ----------
        trans : class
            A fitted UMAP class.

        Returns
        -------
        emb :
            UMAP embedding
        r_orig
            original radii
        r_emb
            embedded radii

        See Also
        --------
        umap.UMAP : UMAP class.
        """
        emb, r_orig_log, r_emb_log = attrgetter("embedding_", "rad_orig_", "rad_emb_")(
            trans
        )
        r_orig = np.exp(r_orig_log)
        r_emb = np.exp(r_emb_log)
        return emb, r_orig, r_emb

    def mvn_prob_sum(self, emb, r_orig, n=100):
        """Gridded multivariate normal probability summation.

        Parameters
        ----------
        emb : ndarray
            Clustering embedding.
        r_orig : 1d array
            Original DensMAP radii.
        n : int, optional
            Number of points along the x and y axes (total grid points = n^2), by default 100

        Returns
        -------
        x : 1d array
            x-coordinates
        y : 1d array
            y-coordinates
        pdf_sum : 1d array
            summed densities at the (`x`, `y`) locations
        """
        # multivariate normal probability summation
        mn = np.amin(emb, axis=0)
        mx = np.amax(emb, axis=0)
        x, y = np.mgrid[mn[0] : mx[0] : n * 1j, mn[1] : mx[1] : n * 1j]  # type: ignore
        pos = np.dstack((x, y))

        with self.Timer("pdf-summation"):
            mvn_list = list(map(my_mvn, emb[:, 0], emb[:, 1], r_orig))
            pdf_list = [mvn.pdf(pos) for mvn in mvn_list]
            pdf_sum = np.sum(pdf_list, axis=0)
        return x, y, pdf_sum

    def compute_log_density(self, r_orig=None):
        """Compute the log density based on the radii.

        Parameters
        ----------
        r_orig : 1d array, optional
            The original radii associated with the fitted DensMAP, by default None. If
            None, then defaults to self.std_r_orig.

        Returns
        -------
        self.dens, self.log_dens : 1d array
            Densities and log densities associated with the original radii, respectively.

        Notes
        -----
        Density is approximated as 1/r_orig
        """
        if r_orig is None:
            r_orig = self.std_r_orig
        self.dens = 1 / r_orig
        self.log_dens = np.log(self.dens)
        return self.dens, self.log_dens

    def plot(self, return_pareto_ind=False):
        """Plot and save various cluster and Pareto front figures.

        Parameters
        ----------
        return_pareto_ind : bool, optional
            Whether to return the pareto front indices, by default False

        Returns
        -------
        pk_pareto_ind, dens_pareto_ind : tuple of int
            Pareto front indices for the peak and density proxies, respectively.
        """
        # create dir https://stackoverflow.com/a/273227/13697228
        Path(self.figure_dir).mkdir(parents=True, exist_ok=True)

        # peak pareto plot setup
        x = str(self.n_neighbors) + "_neigh_avg_targ (GPa)"
        y = "target (GPa)"
        # TODO: plot for val data only (fixed?)
        peak_df = pd.DataFrame(
            {
                x: self.val_k_neigh_avg,
                y: self.val_pred,
                "formula": self.val_formula,
                "Peak height (GPa)": self.val_pred - self.val_k_neigh_avg,
                "cluster ID": self.val_labels,
            }
        )
        # peak pareto plot
        # TODO: double check if cluster labels are correct between the two pareto plots
        fig, pk_pareto_ind = pareto_plot(
            peak_df,
            x=x,
            y=y,
            color="cluster ID",
            fpath=join(self.figure_dir, "pf-peak-proxy"),
            pareto_front=True,
        )

        x = "log validation density"
        y = "validation predictions (GPa)"
        # cluster-wise average vs. cluster-wise validation log-density
        frac_df = pd.DataFrame(
            {
                x: self.val_log_dens.ravel(),
                y: self.val_pred.ravel(),
                "cluster ID": self.val_labels,
                "formula": self.val_formula,
            }
        )
        # FIXME: manually set the lower and upper bounds of the cmap here (or convert to dict)
        fig, frac_pareto_ind = pareto_plot(
            frac_df,
            x=x,
            y=y,
            color="cluster ID",
            fpath=join(self.figure_dir, "pf-train-contrib-proxy"),
            pareto_front=True,
            parity_type=None,
        )

        # Scatter plot colored by clusters
        fig = umap_cluster_scatter(self.std_emb, self.labels)

        # Histogram of cluster counts
        fig = cluster_count_hist(self.labels)

        # Scatter plot colored by target values
        fig = target_scatter(self.std_emb, self.all_target)

        # PDF evaluated on grid of points
        if self.pdf:
            fig = dens_scatter(self.pdf_x, self.pdf_y, self.pdf_sum)

            fig = dens_targ_scatter(
                self.std_emb, self.all_target, self.pdf_x, self.pdf_y, self.pdf_sum
            )

        # dens pareto plot setup
        dens, log_dens = self.compute_log_density()

        x = "log density"
        y = "target (GPa)"
        dens_df = pd.DataFrame(
            {
                x: log_dens,
                y: self.all_target,
                "formula": self.all_formula,
                "cluster ID": self.labels,
            }
        )
        # dens pareto plot
        # FIXME: manually set the lower and upper bounds of the cmap here (or convert to dict)
        # TODO: make the colorscale discrete
        fig, dens_pareto_ind = pareto_plot(
            dens_df,
            x=x,
            y=y,
            fpath=join(self.figure_dir, "pf-dens-proxy"),
            parity_type=None,
            color="cluster ID",
            pareto_front=True,
        )

        x = "cluster-wise validation fraction"
        y = "cluster-wise average target (GPa)"
        # cluster-wise average vs. cluster-wise validation fraction
        frac_df = pd.DataFrame(
            {
                x: self.val_frac.ravel(),
                y: self.cluster_avg.ravel(),
                "cluster ID": self.unique_labels,
            }
        )
        fig, frac_pareto_ind = pareto_plot(
            frac_df,
            x=x,
            y=y,
            hover_data=None,
            color="cluster ID",
            fpath=join(self.figure_dir, "pf-frac-proxy"),
            pareto_front=True,
            reverse_x=False,
            parity_type=None,
            xrange=[0, 1],
        )

        # Group cross-validation parity plot
        if self.true_avg_targ is not None:
            # x = "$E_\\mathrm{avg,true}$ (GPa)"
            # y = "$E_\\mathrm{avg,pred}$ (GPa)"
            x = "true cluster avg target (GPa)"
            y = "pred cluster avg target (GPa)"
            gcv_df = pd.DataFrame(
                {
                    x: self.true_avg_targ,
                    y: self.pred_avg_targ,
                    "formula": None,
                    "cluster ID": np.array(self.avg_labels),
                }
            )
            fig, dens_pareto_ind = pareto_plot(
                gcv_df,
                x=x,
                y=y,
                fpath=join(self.figure_dir, "gcv-pareto"),
                parity_type="max-of-both",
                color="cluster ID",
                pareto_front=False,
                reverse_x=False,
            )
            # fig = group_cv_parity(
            #     self.true_avg_targ, self.pred_avg_targ, self.avg_labels
            # )
        else:
            warn("Skipping group cross-validation plot")

        if return_pareto_ind:
            return pk_pareto_ind, dens_pareto_ind

    # TODO: write function to visualize Wasserstein metric (barchart with height = color)

    def save(self, fpath="disc.pkl", dummy=False):
        """Save Discover model.

        Parameters
        ----------
        fpath : str, optional
            Filepath to which to save, by default "disc.pkl"

        See Also
        --------
        load : load a Discover model.
        """
        if dummy is True:
            warn("Dummy flag set to True. Overwriting fpath to dummy_disc.pkl")
            fpath = "dummy_disc.pkl"

        with open(fpath, "wb") as f:
            pickle.dump(self, f)

    def load(self, fpath="disc.pkl"):
        """Load Discover model.

        Parameters
        ----------
        fpath : str, optional
            Filepath from which to load, by default "disc.pkl"

        Returns
        -------
        Class
            Loaded Discover() model.
        """
        with open(fpath, "rb") as f:
            disc = pickle.load(f)
            return disc

    def data(
        self,
        module,
        fname="train.csv",
        groupby=True,
        dummy=False,
        split=True,
        val_size=0.2,
        test_size=0.0,
        random_state=42,
    ):
        """Grab data from within the subdirectories (modules) of mat_discover.

        Parameters
        ----------
        module : Module
            The module within mat_discover that contains e.g. "train.csv". For example,
            from crabnet.data.materials_data import elasticity
        fname : str, optional
            Filename of text file to open.
        dummy : bool, optional
            Whether to pare down the data to a small test set, by default False
        groupby : bool, optional
            Whether to use groupby_formula to filter identical compositions
        split : bool, optional
            Whether to split the data into train, val, and (optionally) test sets, by default True
        val_size : float, optional
            Validation dataset fraction, by default 0.2
        test_size : float, optional
            Test dataset fraction, by default 0.0
        random_state : int, optional
            seed to use for the train/val/test split, by default 42

        Returns
        -------
        DataFrame
            If split==False, then the full DataFrame is returned directly

        DataFrame, DataFrame
            If test_size == 0 and split==True, then training and validation DataFrames are returned.

        DataFrame, DataFrame, DataFrame
            If test_size > 0 and split==True, then training, validation, and test DataFrames are returned.
        """
        train_csv = open_text(module, fname)
        df = pd.read_csv(train_csv)

        if groupby:
            df = groupby_formula(df, how="max")

        if dummy:
            ntot = min(100, len(df))
            df = df.head(ntot)

        if split:
            if test_size > 0:
                df, test_df = train_test_split(
                    df, test_size=test_size, random_state=random_state
                )

            train_df, val_df = train_test_split(
                df, test_size=val_size / (1 - test_size), random_state=random_state
            )

            if test_size > 0:
                return train_df, val_df, test_df
            else:
                return train_df, val_df
        else:
            return df
