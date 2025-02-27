import os
from matplotlib import shutil
import torch
from torch import Tensor
from torch.utils.data import DataLoader, Dataset
import numpy as np

from utils.Engine import Engine
from utils.audiolib import audiowrite
from utils.ini_opts import read_ini
from utils.trunk import AECTrunk
from utils.record import REC, RECDepot
from utils.losses import loss_compressed_mag, loss_sisnr, loss_pmsqe

from utils.stft_loss import MultiResolutionSTFTLoss

# from utils.conv_stft_loss import MultiResolutionSTFTLoss
from tqdm import tqdm
from typing import Dict, Optional, Union, List
from models.APC_SNR.apc_snr import APC_SNR_multi_filter
from models.conv_stft import STFT
from models.dpcrn_refinement import DPCRN_REFINEMENT
from models.IMDPCRN import (
    CRN_AEC,
    BaseCRN,
    BaseCRN_2,
    BaseCRNDensePWC,
    BaseCRNwSubBands,
    DFCRN,
    DFCRN_pwc,
    BaseCRNwGroupFT,
    CRN_AEC_2,
)

# from models.pase.models.frontend import wf_builder
import argparse


class Train(Engine):
    def __init__(
        self,
        train_dset: Dataset,
        valid_dset: Dataset,
        vtest_dset: Union[List[Dataset], Dataset],
        batch_sz: int,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.train_loader = DataLoader(
            train_dset,
            batch_size=batch_sz,
            num_workers=6,
            pin_memory=True,
            shuffle=True,
            worker_init_fn=self._worker_set_seed,
            generator=self._set_generator(),
        )
        # g = torch.Generator()
        # g.manual_seed(0)
        self.valid_loader = DataLoader(
            valid_dset,
            batch_size=batch_sz,
            num_workers=6,
            pin_memory=True,
            shuffle=True,
            worker_init_fn=self._worker_set_seed,
            generator=self._set_generator(),
            # generator=g,
        )
        self.valid_dset = valid_dset

        self.draw_epoch = kwargs.get("draws_per_valid_int", 0)

        self.vtest_loader: List

        self.vtest_loader = (
            [vtest_dset] if isinstance(vtest_dset, Dataset) else vtest_dset
        )

        self.stft = STFT(nframe=512, nhop=256).to(self.device)
        self.stft.eval()

        # self.ms_stft_loss = MultiResolutionSTFTLoss(
        #     fft_sizes=[960, 480, 240],
        #     hop_sizes=[480, 240, 120],
        #     win_lengths=[960, 480, 240],
        # ).to(self.device)
        self.ms_stft_loss = MultiResolutionSTFTLoss(
            fft_sizes=[1024, 512, 256],
            hop_sizes=[512, 256, 128],
            win_lengths=[1024, 512, 256],
        ).to(self.device)
        self.ms_stft_loss.eval()

        # self.APC_criterion = APC_SNR_multi_filter(
        #     model_hop=128,
        #     model_winlen=512,
        #     mag_bins=256,
        #     theta=0.01,
        #     hops=[8, 16, 32, 64],
        # ).to(self.device)

        # self.pase = wf_builder("config/frontend/PASE+.cfg").eval()
        # self.pase.load_pretrained(
        #     "./pretrained/pase_e199.ckpt", load_last=True, verbose=True
        # )
        # self.pase.to("cuda")
        self.raw_metrics = self._load_dsets_metrics()

    @staticmethod
    def _config_optimizer(name: str, params, **kwargs):
        return super(Train, Train)._config_optimizer(
            name, filter(lambda p: p.requires_grad, params, **kwargs)
        )

    def _valid_dsets(self):
        dset_dict = {}
        # -----------------------#
        ##### valid dataset  #####
        # -----------------------#
        metric_rec = REC()
        pbar = tqdm(
            self.valid_loader,
            ncols=120,
            leave=False,
            desc=f"v-{self.valid_dset.dirname}",
        )

        for mic, ref, sph, scenario in pbar:
            mic = mic.to(self.device)  # b,c,t,f
            ref = ref.to(self.device)  # b,c,t,f
            sph = sph.to(self.device)  # b,c,t,f
            sce = scenario.to(self.device)  # b,1

            metric_dict = self.valid_fn(sph, mic, sce, return_loss=False)
            metric_dict.pop("score")
            # record the loss
            metric_rec.update(metric_dict)
            pbar.set_postfix(**metric_rec.state_dict())

        dset_dict["valid"] = metric_rec.state_dict()

        return dset_dict

    def loss_fn(self, clean: Tensor, enh: Tensor) -> Dict:
        # apc loss
        # loss_APC_SNR, loss_pmsqe = self.APC_criterion(enh + 1e-8, clean + 1e-8)
        # loss = 0.05 * loss_APC_SNR + loss_pmsqe  # + loss_pase
        # return {
        #     "loss": loss,
        #     "pmsqe": loss_pmsqe.detach(),
        #     "apc_snr": loss_APC_SNR.detach(),
        # }

        # sisnr_lv = loss_sisnr(clean, enh)
        # specs_enh = self.stft.transform(enh)
        # specs_sph = self.stft.transform(clean)
        # mse_mag, mse_pha = loss_compressed_mag(specs_sph, specs_enh)
        # pmsqe_score = loss_pmsqe(specs_sph, specs_enh)
        # loss = 0.05 * sisnr_lv + mse_pha + mse_mag + pmsq_score
        # return {
        #     "loss": loss,
        #     "sisnr": sisnr_lv.detach(),
        #     "mag": mse_mag.detach(),
        #     "pha": mse_pha.detach(),
        #     "pmsq": pmsq_score.detach(),
        # }

        sc_loss, mag_loss = self.ms_stft_loss(enh, clean)
        loss = sc_loss + mag_loss  # + 0.05 * pmsqe_score
        return {
            "loss": loss,
            "sc": sc_loss.detach(),
            "mag": mag_loss.detach(),
            # "pmsq": pmsqe_score.detach(),
        }

        # pase loss
        # clean = clean.unsqueeze(1)
        # enh = enh.unsqueeze(1)
        # clean_pase = self.pase(clean)
        # clean_pase = clean_pase.flatten(1)
        # enh_pase = self.pase(enh)
        # enh_pase = enh_pase.flatten(1)
        # loss_pase = F.mse_loss(clean_pase, enh_pase)

    def _fit_each_epoch(self, epoch):
        losses_rec = REC()

        pbar = tqdm(
            self.train_loader,
            ncols=120,
            leave=True,
            desc=f"Epoch-{epoch}/{self.epochs}",
        )
        for mic, ref, sph, _ in pbar:
            mic = mic.to(self.device)  # b,c,t,f
            ref = ref.to(self.device)  # b,c,t,f
            sph = sph.to(self.device)  # b,c,t,f

            self.optimizer.zero_grad()
            enh = self.net(mic, ref)
            loss_dict = self.loss_fn(sph[:, : enh.shape[-1]], enh)

            loss = loss_dict["loss"]
            loss.backward()

            # torch.nn.utils.clip_grad_norm_(self.net.parameters(), 3, 2)
            self.optimizer.step()

            # record the loss
            losses_rec.update(loss_dict)
            pbar.set_postfix(**losses_rec.state_dict())

        return losses_rec.state_dict()

    def valid_fn(
        self, sph: Tensor, enh: Tensor, scenario: Tensor, return_loss: bool = True
    ) -> Dict:
        sisnr = self._si_snr(sph, enh)
        sisnr = np.mean(sisnr)

        pesq = self._pesq(
            sph.cpu().detach().numpy(),
            enh.cpu().detach().numpy(),
            fs=16000,
            norm=False,
        )
        pesq = np.mean(pesq)
        # composite = self._eval(clean, enh, 16000)
        # composite = {k: np.mean(v) for k, v in composite.items()}
        # pesq = composite.pop("pesq")

        stoi = self._stoi(
            sph.cpu().detach().numpy(),
            enh.cpu().detach().numpy(),
            fs=16000,
        )
        stoi = np.mean(stoi)

        score = (pesq - 1) / 3.5 + stoi

        state = {"score": score, "pesq": pesq, "stoi": stoi, "sisnr": sisnr}

        if return_loss:
            loss_dict = self.loss_fn(sph, enh)

            # return dict(state, **composite)
            return dict(state, **loss_dict)
        else:
            return state

    def _valid_each_epoch(self, epoch):
        metric_rec = REC()

        pbar = tqdm(
            self.valid_loader,
            ncols=120,
            leave=True,
            desc=f"Valid-{epoch}/{self.epochs}",
        )

        draw = True

        for mic, ref, sph, scenario in pbar:
            mic = mic.to(self.device)  # b,c,t,f
            ref = ref.to(self.device)  # b,c,t,f
            sph = sph.to(self.device)  # b,c,t,f
            sce = scenario.to(self.device)  # b,1

            with torch.no_grad():
                enh = self.net(mic, ref)

            metric_dict = self.valid_fn(sph[:, : enh.shape[-1]], enh, sce)

            if draw is True and self.draw_epoch % epoch == 0 and self.draw_epoch != 0:
                with torch.no_grad():
                    mxk = self.stft.transform(mic)
                    exk = self.stft.transform(enh)
                    sxk = self.stft.transform(sph)
                self._draw_spectrogram(
                    epoch, mxk, exk, sxk, titles=("mic", "enh", "sph")
                )
                draw = False

            # record the loss
            metric_rec.update(metric_dict)
            pbar.set_postfix(**metric_rec.state_dict())

        out = {}
        for k, v in metric_rec.state_dict().items():
            if k in self.raw_metrics["valid"]:
                out[k] = {"enh": v, "raw": self.raw_metrics["valid"][k]}
            else:
                out[k] = v
        # return metric_rec.state_dict()
        return out

    def vtest_fn(self, sph: Tensor, enh: Tensor) -> Dict:
        sisnr = self._si_snr(sph, enh)
        sisnr = np.mean(sisnr)

        pesq = self._pesq(
            sph.cpu().detach().numpy(),
            enh.cpu().detach().numpy(),
            fs=16000,
            norm=False,
        )
        pesq = np.mean(pesq)
        # composite = self._eval(clean, enh, 16000)
        # composite = {k: np.mean(v) for k, v in composite.items()}
        # pesq = composite.pop("pesq")

        stoi = self._stoi(
            sph.cpu().detach().numpy(),
            enh.cpu().detach().numpy(),
            fs=16000,
        )
        stoi = np.mean(stoi)

        state = {"pesq": pesq, "stoi": stoi, "sisnr": sisnr}

        # return dict(state, **composite)
        return state

    def vtest_aecmos(self, src_dir, out_dir):
        import json

        out = os.popen(f"python scripts/aecmos.py --src {src_dir}  --est {out_dir}")
        return json.loads(out.read())

    def _vtest_each_epoch(self, epoch):
        vtest_metric = {}

        for vtest_loader in self.vtest_loader:
            metric_rec = RECDepot()
            use_aecmos = False

            dirname = os.path.split(vtest_loader.dirname)[-1]
            pbar = tqdm(
                vtest_loader,
                total=len(vtest_loader),
                ncols=120,
                leave=False,
                desc=f"vTest-{epoch}/{dirname}",
            )
            vtest_outdir = os.path.join(self.vtest_outdir, dirname)
            shutil.rmtree(vtest_outdir) if os.path.exists(vtest_outdir) else None

            for mic, ref, sph, fname in pbar:
                mic = mic.to(self.device)  # b,c,t,f
                ref = ref.to(self.device)  # b,c,t,f
                sph = sph.to(self.device) if sph is not None else None  # b,c,t,f

                item = os.path.split(os.path.dirname(fname))[-1]

                with torch.no_grad():
                    enh = self.net(mic, ref)

                audiowrite(
                    os.path.join(self.vtest_outdir, fname),
                    enh.cpu().squeeze().numpy(),
                    16000,
                )

                if sph is None:
                    use_aecmos = True
                    continue

                metric_dict = self.vtest_fn(sph[:, : enh.shape[-1]], enh)
                # record the loss
                metric_rec.update(item, metric_dict)
                # pbar.set_postfix(**metric_rec.state_dict())

            if use_aecmos:
                metric_dict = self.vtest_aecmos(vtest_loader.dirname, vtest_outdir)
                vtest_metric[dirname] = metric_dict
            else:
                vtest_metric[dirname] = metric_rec.state_dict()

        return vtest_metric

    def _net_flops(self) -> int:
        from thop import profile
        import copy

        x = torch.randn(1, 16000)
        flops, _ = profile(
            copy.deepcopy(self.net).cpu(),
            inputs=(x, x),
            verbose=False,
        )
        return flops


def parse():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="crn aec model", action="store_true")
    parser.add_argument("--base-2", help="crn aec model", action="store_true")
    parser.add_argument("--base-pwc", help="crn aec model", action="store_true")
    parser.add_argument(
        "--test", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--crn", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--crn-2", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wsub", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wdf", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wdf-pwc", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wgroup", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wfusion-att", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wfusion-ms", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wfusion-dilated", help="fusion with the atten method", action="store_true"
    )
    parser.add_argument(
        "--wfusion-refine", help="fusion with the refine method", action="store_true"
    )
    parser.add_argument("-T", "--train", help="train mode", action="store_true")

    parser.add_argument("--ckpt", help="ckpt path", type=str)
    parser.add_argument("--src", help="input directory", type=str)
    parser.add_argument("--dst", help="predicting output directory", type=str)
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse()

    if args.base:  # 31
        cfg_fname = "config/config_imdpcrn_base.ini"
        cfg = read_ini(cfg_fname)
        net = BaseCRN(64)
    elif args.base_pwc:  # 31
        cfg_fname = "config/config_imdpcrn_base_pwc.ini"
        cfg = read_ini(cfg_fname)
        net = BaseCRNDensePWC(64)
    elif args.base_2:  # 31
        cfg_fname = "config/config_imdpcrn_base_2.ini"
        cfg = read_ini(cfg_fname)
        net = BaseCRN_2(64)
    elif args.crn:  # 31
        cfg_fname = "config/config_imdpcrn_crn.ini"
        cfg = read_ini(cfg_fname)
        net = CRN_AEC(
            nframe=512,
            nhop=256,
            nfft=512,
            cnn_num=[32, 64, 128, 128],
            stride=[2, 2, 1, 1],
            rnn_hidden_num=128,
        )
    elif args.crn_2:  # 31
        cfg_fname = "config/config_imdpcrn_crn_2.ini"
        cfg = read_ini(cfg_fname)
        net = CRN_AEC_2(
            nframe=512,
            nhop=256,
            nfft=512,
            cnn_num=[32, 64, 128, 128],
            stride=[2, 2, 1, 1],
            rnn_hidden_num=128,
        )
    elif args.wsub:  # 31
        cfg_fname = "config/config_imdpcrn_sub.ini"
        cfg = read_ini(cfg_fname)
        net = BaseCRNwSubBands(64, sub=(3, 4))
    elif args.wdf:  # 31
        cfg_fname = "config/config_imdpcrn_df.ini"
        cfg = read_ini(cfg_fname)
        net = DFCRN(64)
    elif args.wdf_pwc:  # 31
        cfg_fname = "config/config_imdpcrn_df_pwc.ini"
        cfg = read_ini(cfg_fname)
        net = DFCRN_pwc(64)
    elif args.wgroup:  # 31
        cfg_fname = "config/config_imdpcrn_group.ini"
        cfg = read_ini(cfg_fname)
        net = BaseCRNwGroupFT(64)
    else:  # 212  baseline
        cfg_fname = "config/config_adpcrn.ini"
        cfg = read_ini(cfg_fname)

    print("\033[35m" + cfg_fname + "\033[0m")

    vtests = list(
        map(lambda x: x.strip("\n"), cfg["dataset"]["vtest_dset"].strip(",").split(","))
    )

    vtests = [
        AECTrunk(
            dirname,
            patten="**/*mic.wav",
            keymap=("mic", "lpb", "sph"),
            align=True,
        )
        for dirname in vtests
    ]

    init = cfg["config"]
    eng = Train(
        AECTrunk(
            cfg["dataset"]["train_dset"],
            patten="**/*mic.wav",
            keymap=("mic", "ref", "sph"),
            align=True,
        ),
        AECTrunk(
            cfg["dataset"]["valid_dset"],
            # flist="gene-aec-4-1.csv",
            patten="**/*mic.wav",
            keymap=("mic", "ref", "sph"),
            align=True,
        ),
        vtests,
        net=net,
        batch_sz=4,
        valid_first=False,
        **init,
    )
    print(eng)
    if args.test:
        eng.test()
    else:
        eng.fit()
