# coding: utf-8
# Author：WangTianRui
# Date ：2021/4/4 20:29
import torch
import models.APC_SNR.acoustic_scaler as acoustic_scaler
import models.APC_SNR.pmsqe.pmsqe as pmsqe
import models.APC_SNR.conv_stft as conv_stft


def power(x):
    """
    :param x:B,2*F,T
    :return:
    """
    return torch.stack(torch.chunk(x, 2, dim=-2), dim=-1).pow(2).sum(dim=-1)


def stft_snr(est, clean, eps=1e-8):
    s1 = est.reshape(est.size(0), -1)
    s2 = clean.reshape(clean.size(0), -1)
    s1_s2_norm = torch.sum(s1 * s2, -1, keepdim=True)
    s2_s2_norm = torch.sum(s2 * s2, -1, keepdim=True)
    s_target = s1_s2_norm / (s2_s2_norm + eps) * s2
    e_nosie = s1 - s_target
    target_norm = torch.sum(s_target * s_target, -1, keepdim=True)
    noise_norm = torch.sum(e_nosie * e_nosie, -1, keepdim=True)
    snr = 10 * torch.log10(target_norm / (noise_norm + eps) + eps)
    return -(torch.mean(snr))


class APC_SNR_multi_filter(torch.nn.Module):
    # APC_SNR_multi_filter is better than APC_SNR with more GPU memory.
    # criterion = APC_SNR_multi_filter(model_hop=128, model_winlen=512, mag_bins=256, theta=0.01, hops=[8, 16, 32, 64])
    def __init__(self, model_hop, model_winlen, theta, mag_bins, hops=()):
        super(APC_SNR_multi_filter, self).__init__()
        self.multi_hop = hops
        self.scaler = acoustic_scaler.AcousticScaler(
            theta=theta, mag_bins=(mag_bins + 1), no_grad=True
        )
        self.eps = 1e-8
        self.pmsqe = pmsqe.SingleSrcPMSQE(sample_rate=16000)
        self.model_stft = conv_stft.ConvSTFT(
            model_winlen, model_hop, mag_bins * 2, "hanning sqrt", "complex"
        )
        self.model_istft = conv_stft.ConviSTFT(
            model_winlen, model_hop, mag_bins * 2, "hanning sqrt", "complex"
        )
        if model_hop in self.multi_hop:
            self.multi_hop.remove(model_hop)
        self.stfts = torch.nn.ModuleList()
        for hop in self.multi_hop:
            self.stfts.append(
                conv_stft.ConvSTFT(
                    model_winlen, hop, mag_bins * 2, "hanning sqrt", "complex"
                )
            )

    def forward(self, est_time_domain, clean):
        """
        :param est: B,2*F,T
        :param clean: B,T
        :return:
        """
        # est_time_domain = self.model_istft(est).squeeze(1)  # B,T

        est = self.model_stft(est_time_domain)  # B,2*F,T
        clean_stft = self.model_stft(clean)  # B,2*F,T

        pmsqe_score = self.pmsqe_score(est, clean_stft)

        est_scales, clean_scales = self.scaler(est, clean_stft)
        est = est * est_scales
        clean_stft = clean_stft * clean_scales
        stft_scaled_snr = stft_snr(est, clean_stft)

        for filter in self.stfts:
            clean_stft = filter(clean)
            est_stft = filter(est_time_domain)

            pmsqe_score += self.pmsqe_score(est_stft, clean_stft)
            est_scales, clean_scales = self.scaler(est_stft, clean_stft)
            stft_scaled_snr += stft_snr(
                est_stft * est_scales, clean_stft * clean_scales
            )

        return stft_scaled_snr / (len(self.stfts) + 1), pmsqe_score / (
            len(self.stfts) + 1
        )

    def pmsqe_score(self, est_stft, clean_stft):
        mag_est = power(est_stft)
        mag_clean = power(clean_stft)
        pmsqe_score = self.pmsqe(mag_est, mag_clean)
        return pmsqe_score.mean()


class APC_SNR(torch.nn.Module):
    def __init__(self, model_hop, model_winlen, theta, mag_bins):
        super(APC_SNR, self).__init__()

        self.winlen = model_winlen
        self.hop = model_hop
        self.scaler = acoustic_scaler.AcousticScaler(
            theta=theta, mag_bins=(mag_bins + 1), no_grad=True
        )
        self.eps = 1e-8
        self.pmsqe = pmsqe.SingleSrcPMSQE(sample_rate=16000)
        self.model_stft = conv_stft.ConvSTFT(
            model_winlen, model_hop, mag_bins * 2, "hanning sqrt", "complex"
        )

    def forward(self, est_time_domain, clean, mix_vad):
        """
        :param est: B,2*F,T
        :param clean: B,T
        :return:
        """
        clean_stft = self.model_stft(clean)  # B,2*F,T
        est = self.model_stft(est_time_domain)  # B,2*F,T

        #### compute vad labels
        frame_num = 1 + (mix_vad.shape[1] - self.winlen) // self.hop
        frames = torch.zeros([mix_vad.shape[0], frame_num, self.winlen])

        for index in range(frame_num):
            frames[:, index, :] = mix_vad[
                :, index * self.hop : (index * self.hop + self.winlen)
            ]

        vad_frames = torch.sum(frames, dim=2, keepdim=True)

        ones_vec = torch.ones_like(vad_frames)
        zeros_vec = torch.ones_like(vad_frames) * 0.1

        vad_label = torch.where(vad_frames > 0, ones_vec, zeros_vec).to("cuda")

        vad_mask = torch.zeros(vad_label.shape[0], est.shape[2], 1).to("cuda")
        vad_mask[:, : vad_label.shape[1], :] = vad_label

        pmsqe_score = self.pmsqe_score(est, clean_stft, vad_mask)
        est_scales, clean_scales = self.scaler(est, clean_stft)
        est = est * est_scales
        clean_stft = clean_stft * clean_scales
        stft_scaled_snr = stft_snr(est, clean_stft)
        return stft_scaled_snr, pmsqe_score

    def pmsqe_score(self, est_stft, clean_stft, vad_mask):
        mag_est = power(est_stft)
        mag_clean = power(clean_stft)
        pmsqe_score = self.pmsqe(mag_est, mag_clean, vad_mask)
        return pmsqe_score.mean()
