import torch
from nnunetv2.utilities.plans_handling.plans_handler import ConfigurationManager, PlansManager

from nnunetv2.training.lr_scheduler.polylr import PolyLRScheduler
from torch import nn
from nnunetv2.training.nnUNetTrainer.nnUNetTrainer import nnUNetTrainer
from torch.nn import functional as F
import torch.utils.checkpoint as checkpoint
from torch._dynamo import OptimizedModule


class MedNeXtBlock(nn.Module):

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 exp_r: int = 4,
                 kernel_size: int = 7,
                 do_res: int = True,
                 norm_type: str = 'group',
                 n_groups: int or None = None,
                 dim='3d',
                 grn=False
                 ):

        super().__init__()

        self.do_res = do_res

        assert dim in ['2d', '3d']
        self.dim = dim
        if self.dim == '2d':
            conv = nn.Conv2d
        elif self.dim == '3d':
            conv = nn.Conv3d

        # First convolution layer with DepthWise Convolutions
        self.conv1 = conv(
            in_channels=in_channels,
            out_channels=in_channels,
            kernel_size=kernel_size,
            stride=1,
            padding=kernel_size // 2,
            groups=in_channels if n_groups is None else n_groups,
        )

        # Normalization Layer. GroupNorm is used by default.
        if norm_type == 'group':
            self.norm = nn.GroupNorm(
                num_groups=in_channels,
                num_channels=in_channels
            )
        elif norm_type == 'layer':
            self.norm = LayerNorm(
                normalized_shape=in_channels,
                data_format='channels_first'
            )

        # Second convolution (Expansion) layer with Conv3D 1x1x1
        self.conv2 = conv(
            in_channels=in_channels,
            out_channels=exp_r * in_channels,
            kernel_size=1,
            stride=1,
            padding=0
        )

        # GeLU activations
        self.act = nn.GELU()

        # Third convolution (Compression) layer with Conv3D 1x1x1
        self.conv3 = conv(
            in_channels=exp_r * in_channels,
            out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0
        )

        self.grn = grn
        if grn:
            if dim == '3d':
                self.grn_beta = nn.Parameter(torch.zeros(1, exp_r * in_channels, 1, 1, 1), requires_grad=True)
                self.grn_gamma = nn.Parameter(torch.zeros(1, exp_r * in_channels, 1, 1, 1), requires_grad=True)
            elif dim == '2d':
                self.grn_beta = nn.Parameter(torch.zeros(1, exp_r * in_channels, 1, 1), requires_grad=True)
                self.grn_gamma = nn.Parameter(torch.zeros(1, exp_r * in_channels, 1, 1), requires_grad=True)

    def forward(self, x, dummy_tensor=None):

        x1 = x
        x1 = self.conv1(x1)
        x1 = self.act(self.conv2(self.norm(x1)))
        if self.grn:
            # gamma, beta: learnable affine transform parameters
            # X: input of shape (N,C,H,W,D)
            if self.dim == '3d':
                gx = torch.norm(x1, p=2, dim=(-3, -2, -1), keepdim=True)
            elif self.dim == '2d':
                gx = torch.norm(x1, p=2, dim=(-2, -1), keepdim=True)
            nx = gx / (gx.mean(dim=1, keepdim=True) + 1e-6)
            x1 = self.grn_gamma * (x1 * nx) + self.grn_beta + x1
        x1 = self.conv3(x1)
        if self.do_res:
            x1 = x + x1
        return x1


class MedNeXtDownBlock(MedNeXtBlock):

    def __init__(self, in_channels, out_channels, exp_r=4, kernel_size=7,
                 do_res=False, norm_type='group', dim='3d', grn=False):

        super().__init__(in_channels, out_channels, exp_r, kernel_size,
                         do_res=False, norm_type=norm_type, dim=dim,
                         grn=grn)

        if dim == '2d':
            conv = nn.Conv2d
        elif dim == '3d':
            conv = nn.Conv3d
        self.resample_do_res = do_res
        if do_res:
            self.res_conv = conv(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=1,
                stride=2
            )

        self.conv1 = conv(
            in_channels=in_channels,
            out_channels=in_channels,
            kernel_size=kernel_size,
            stride=2,
            padding=kernel_size // 2,
            groups=in_channels,
        )

    def forward(self, x, dummy_tensor=None):

        x1 = super().forward(x)

        if self.resample_do_res:
            res = self.res_conv(x)
            x1 = x1 + res

        return x1


class MedNeXtUpBlock(MedNeXtBlock):

    def __init__(self, in_channels, out_channels, exp_r=4, kernel_size=7,
                 do_res=False, norm_type='group', dim='3d', grn=False):
        super().__init__(in_channels, out_channels, exp_r, kernel_size,
                         do_res=False, norm_type=norm_type, dim=dim,
                         grn=grn)

        self.resample_do_res = do_res

        self.dim = dim
        if dim == '2d':
            conv = nn.ConvTranspose2d
        elif dim == '3d':
            conv = nn.ConvTranspose3d
        if do_res:
            self.res_conv = conv(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=1,
                stride=2
            )

        self.conv1 = conv(
            in_channels=in_channels,
            out_channels=in_channels,
            kernel_size=kernel_size,
            stride=2,
            padding=kernel_size // 2,
            groups=in_channels,
        )

    def forward(self, x, dummy_tensor=None):

        x1 = super().forward(x)
        # Asymmetry but necessary to match shape

        if self.dim == '2d':
            x1 = torch.nn.functional.pad(x1, (1, 0, 1, 0))
        elif self.dim == '3d':
            x1 = torch.nn.functional.pad(x1, (1, 0, 1, 0, 1, 0))

        if self.resample_do_res:
            res = self.res_conv(x)
            if self.dim == '2d':
                res = torch.nn.functional.pad(res, (1, 0, 1, 0))
            elif self.dim == '3d':
                res = torch.nn.functional.pad(res, (1, 0, 1, 0, 1, 0))
            x1 = x1 + res

        return x1


class OutBlock(nn.Module):

    def __init__(self, in_channels, n_classes, dim):
        super().__init__()

        if dim == '2d':
            conv = nn.ConvTranspose2d
        elif dim == '3d':
            conv = nn.ConvTranspose3d
        self.conv_out = conv(in_channels, n_classes, kernel_size=1)

    def forward(self, x, dummy_tensor=None):
        return self.conv_out(x)


class LayerNorm(nn.Module):
    """ LayerNorm that supports two data formats: channels_last (default) or channels_first.
    The ordering of the dimensions in the inputs. channels_last corresponds to inputs with
    shape (batch_size, height, width, channels) while channels_first corresponds to inputs
    with shape (batch_size, channels, height, width).
    """

    def __init__(self, normalized_shape, eps=1e-5, data_format="channels_last"):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(normalized_shape))  # beta
        self.bias = nn.Parameter(torch.zeros(normalized_shape))  # gamma
        self.eps = eps
        self.data_format = data_format
        if self.data_format not in ["channels_last", "channels_first"]:
            raise NotImplementedError
        self.normalized_shape = (normalized_shape,)

    def forward(self, x, dummy_tensor=False):
        if self.data_format == "channels_last":
            return F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        elif self.data_format == "channels_first":
            u = x.mean(1, keepdim=True)
            s = (x - u).pow(2).mean(1, keepdim=True)
            x = (x - u) / torch.sqrt(s + self.eps)
            x = self.weight[:, None, None, None] * x + self.bias[:, None, None, None]
            return x


class MedNeXtEncoder(nn.Module):
    def __init__(self,
                 in_channels: int,
                 n_channels: int,
                 exp_r: int = 4,  # Expansion ratio as in Swin Transformers
                 kernel_size: int = 7,  # Ofcourse can test kernel_size
                 do_res: bool = False,  # Can be used to individually test residual connection
                 do_res_up_down: bool = False,  # Additional 'res' connection on up and down convs
                 checkpoint_style: bool = None,  # Either inside block or outside block
                 block_counts: list = [2, 2, 2, 2, 2, 2, 2, 2, 2],  # Can be used to test staging ratio:
                 # [3,3,9,3] in Swin as opposed to [2,2,2,2,2] in nnUNet
                 norm_type='group',
                 dim='3d',  # 2d or 3d
                 grn=False
                 ):
        super().__init__()

        assert checkpoint_style in [None, 'outside_block']
        self.inside_block_checkpointing = False
        self.outside_block_checkpointing = False
        if checkpoint_style == 'outside_block':
            self.outside_block_checkpointing = True
        assert dim in ['2d', '3d']

        if kernel_size is not None:
            enc_kernel_size = kernel_size

        if dim == '2d':
            conv = nn.Conv2d
        elif dim == '3d':
            conv = nn.Conv3d

        self.stem = conv(in_channels, n_channels, kernel_size=1)
        if type(exp_r) == int:
            exp_r = [exp_r for i in range(len(block_counts))]

        self.enc_block_0 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels,
                out_channels=n_channels,
                exp_r=exp_r[0],
                kernel_size=enc_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[0])]
                                         )

        self.down_0 = MedNeXtDownBlock(
            in_channels=n_channels,
            out_channels=2 * n_channels,
            exp_r=exp_r[1],
            kernel_size=enc_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim
        )

        self.enc_block_1 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 2,
                out_channels=n_channels * 2,
                exp_r=exp_r[1],
                kernel_size=enc_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[1])]
                                         )

        self.down_1 = MedNeXtDownBlock(
            in_channels=2 * n_channels,
            out_channels=4 * n_channels,
            exp_r=exp_r[2],
            kernel_size=enc_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.enc_block_2 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 4,
                out_channels=n_channels * 4,
                exp_r=exp_r[2],
                kernel_size=enc_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[2])]
                                         )

        self.down_2 = MedNeXtDownBlock(
            in_channels=4 * n_channels,
            out_channels=8 * n_channels,
            exp_r=exp_r[3],
            kernel_size=enc_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.enc_block_3 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 8,
                out_channels=n_channels * 8,
                exp_r=exp_r[3],
                kernel_size=enc_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[3])]
                                         )

        self.down_3 = MedNeXtDownBlock(
            in_channels=8 * n_channels,
            out_channels=16 * n_channels,
            exp_r=exp_r[4],
            kernel_size=enc_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

    def forward(self, x):
        ret = []
        x = self.stem(x)
        x = self.enc_block_0(x)
        ret.append(x)

        x = self.down_0(x)
        x = self.enc_block_1(x)
        ret.append(x)

        x = self.down_1(x)
        x = self.enc_block_2(x)
        ret.append(x)

        x = self.down_2(x)
        x = self.enc_block_3(x)
        ret.append(x)

        x = self.down_3(x)
        ret.append(x)
        if self.return_skips:
            return ret
        else:
            return ret[-1]

class MedNeXtDecoder(nn.Module):
    def __init__(self, in_channels: int,
                 n_channels: int,
                 n_classes: int,
                 exp_r: int = 4,  # Expansion ratio as in Swin Transformers
                 kernel_size: int = 7,  # Ofcourse can test kernel_size
                 deep_supervision: bool = False,  # Can be used to test deep supervision
                 do_res: bool = False,  # Can be used to individually test residual connection
                 do_res_up_down: bool = False,  # Additional 'res' connection on up and down convs
                 checkpoint_style: bool = None,  # Either inside block or outside block
                 block_counts: list = [2, 2, 2, 2, 2, 2, 2, 2, 2],  # Can be used to test staging ratio:
                 # [3,3,9,3] in Swin as opposed to [2,2,2,2,2] in nnUNet
                 norm_type='group',
                 dim='3d',  # 2d or 3d
                 grn=False):
        super().__init__()

        if type(exp_r) == int:
            exp_r = [exp_r for i in range(len(block_counts))]

        self.do_ds = deep_supervision
        assert checkpoint_style in [None, 'outside_block']
        self.inside_block_checkpointing = False
        self.outside_block_checkpointing = False
        if checkpoint_style == 'outside_block':
            self.outside_block_checkpointing = True
        assert dim in ['2d', '3d']

        if kernel_size is not None:
            dec_kernel_size = kernel_size

        self.bottleneck = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 16,
                out_channels=n_channels * 16,
                exp_r=exp_r[4],
                kernel_size=dec_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[4])]
                                        )

        self.up_3 = MedNeXtUpBlock(
            in_channels=16 * n_channels,
            out_channels=8 * n_channels,
            exp_r=exp_r[5],
            kernel_size=dec_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.dec_block_3 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 8,
                out_channels=n_channels * 8,
                exp_r=exp_r[5],
                kernel_size=dec_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[5])]
                                         )

        self.up_2 = MedNeXtUpBlock(
            in_channels=8 * n_channels,
            out_channels=4 * n_channels,
            exp_r=exp_r[6],
            kernel_size=dec_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.dec_block_2 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 4,
                out_channels=n_channels * 4,
                exp_r=exp_r[6],
                kernel_size=dec_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[6])]
                                         )

        self.up_1 = MedNeXtUpBlock(
            in_channels=4 * n_channels,
            out_channels=2 * n_channels,
            exp_r=exp_r[7],
            kernel_size=dec_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.dec_block_1 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels * 2,
                out_channels=n_channels * 2,
                exp_r=exp_r[7],
                kernel_size=dec_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[7])]
                                         )

        self.up_0 = MedNeXtUpBlock(
            in_channels=2 * n_channels,
            out_channels=n_channels,
            exp_r=exp_r[8],
            kernel_size=dec_kernel_size,
            do_res=do_res_up_down,
            norm_type=norm_type,
            dim=dim,
            grn=grn
        )

        self.dec_block_0 = nn.Sequential(*[
            MedNeXtBlock(
                in_channels=n_channels,
                out_channels=n_channels,
                exp_r=exp_r[8],
                kernel_size=dec_kernel_size,
                do_res=do_res,
                norm_type=norm_type,
                dim=dim,
                grn=grn
            )
            for i in range(block_counts[8])]
                                         )

        self.out_0 = OutBlock(in_channels=n_channels, n_classes=n_classes, dim=dim)

        # Used to fix PyTorch checkpointing bug
        self.dummy_tensor = nn.Parameter(torch.tensor([1.]), requires_grad=True)

        if deep_supervision:
            self.out_1 = OutBlock(in_channels=n_channels * 2, n_classes=n_classes, dim=dim)
            self.out_2 = OutBlock(in_channels=n_channels * 4, n_classes=n_classes, dim=dim)
            self.out_3 = OutBlock(in_channels=n_channels * 8, n_classes=n_classes, dim=dim)
            self.out_4 = OutBlock(in_channels=n_channels * 16, n_classes=n_classes, dim=dim)

        self.block_counts = block_counts
    def forward(self, x, skips):
        x_res_3, x_res_2, x_res_1, x_res_0 = skips
        x = self.bottleneck(x)
        if self.do_ds:
            x_ds_4 = self.out_4(x)

        x_up_3 = self.up_3(x)
        dec_x = x_res_3 + x_up_3
        x = self.dec_block_3(dec_x)

        if self.do_ds:
            x_ds_3 = self.out_3(x)
        del x_res_3, x_up_3

        x_up_2 = self.up_2(x)
        dec_x = x_res_2 + x_up_2
        x = self.dec_block_2(dec_x)
        if self.do_ds:
            x_ds_2 = self.out_2(x)
        del x_res_2, x_up_2

        x_up_1 = self.up_1(x)
        dec_x = x_res_1 + x_up_1
        x = self.dec_block_1(dec_x)
        if self.do_ds:
            x_ds_1 = self.out_1(x)
        del x_res_1, x_up_1

        x_up_0 = self.up_0(x)
        dec_x = x_res_0 + x_up_0
        x = self.dec_block_0(dec_x)
        del x_res_0, x_up_0, dec_x

        x = self.out_0(x)

        if self.do_ds:
            return [x, x_ds_1, x_ds_2, x_ds_3, x_ds_4]
        else:
            return x
class MedNeXt(nn.Module):

    def __init__(self,
                 in_channels: int,
                 n_channels: int,
                 n_classes: int,
                 exp_r: int = 4,  # Expansion ratio as in Swin Transformers
                 kernel_size: int = 7,  # Ofcourse can test kernel_size
                 enc_kernel_size: int = None,
                 dec_kernel_size: int = None,
                 deep_supervision: bool = False,  # Can be used to test deep supervision
                 do_res: bool = False,  # Can be used to individually test residual connection
                 do_res_up_down: bool = False,  # Additional 'res' connection on up and down convs
                 checkpoint_style: bool = None,  # Either inside block or outside block
                 block_counts: list = [2, 2, 2, 2, 2, 2, 2, 2, 2],  # Can be used to test staging ratio:
                 # [3,3,9,3] in Swin as opposed to [2,2,2,2,2] in nnUNet
                 norm_type='group',
                 dim='3d',  # 2d or 3d
                 grn=False
                 ):

        super().__init__()

        self.do_ds = deep_supervision
        assert checkpoint_style in [None, 'outside_block']
        self.inside_block_checkpointing = False
        self.outside_block_checkpointing = False
        if checkpoint_style == 'outside_block':
            self.outside_block_checkpointing = True
        assert dim in ['2d', '3d']

        if kernel_size is not None:
            enc_kernel_size = kernel_size
            dec_kernel_size = kernel_size
        self.encoder = MedNeXtEncoder(in_channels=in_channels, n_channels=n_channels, exp_r=exp_r,
                                      kernel_size=enc_kernel_size, do_res=do_res, do_res_up_down=do_res_up_down,
                                      checkpoint_style=checkpoint_style, block_counts=block_counts,
                                      norm_type=norm_type, dim=dim, grn=grn)
        self.decoder = MedNeXtDecoder(in_channels=in_channels, n_channels=n_channels, n_classes=n_classes,
                                      exp_r=exp_r, kernel_size=dec_kernel_size,deep_supervision=deep_supervision,
                                      do_res=do_res, do_res_up_down=do_res_up_down, checkpoint_style=checkpoint_style,
                                      block_counts=block_counts, norm_type=norm_type, dim=dim, grn=grn)


    def iterative_checkpoint(self, sequential_block, x):
        """
        This simply forwards x through each block of the sequential_block while
        using gradient_checkpointing. This implementation is designed to bypass
        the following issue in PyTorch's gradient checkpointing:
        https://discuss.pytorch.org/t/checkpoint-with-no-grad-requiring-inputs-problem/19117/9
        """
        for l in sequential_block:
            x = checkpoint.checkpoint(l, x, self.dummy_tensor)
        return x

    def forward(self, x):

        x, skips = self.encoder(x)
        out = self.decoder(x, skips)
        return out


class nnUNetTrainer_Optim_and_LR(nnUNetTrainer):

    def __init__(
        self,
        plans: dict,
        configuration: str,
        fold: int,
        dataset_json: dict,
        unpack_dataset: bool = True,
        device: torch.device = torch.device("cuda"),
    ):
        super().__init__(plans, configuration, fold, dataset_json, unpack_dataset, device)
        self.initial_lr = 1e-3
        num_of_outputs_in_mednext = 5
        self.configuration_manager.pool_op_kernel_sizes = [[2, 2, 2] for i in range(num_of_outputs_in_mednext + 1)]

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.network.parameters(),
                                           self.initial_lr,
                                           weight_decay=self.weight_decay,
                                           eps=1e-4  # 1e-8 might cause nans in fp16
                                           )
        lr_scheduler = PolyLRScheduler(optimizer, self.initial_lr, self.num_epochs)
        return optimizer, lr_scheduler


class nnUNetTrainer_MedNeXt_S_kernel3(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True) -> nn.Module:
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=2,  # Expansion ratio as in Swin Transformers
            kernel_size=3,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[2, 2, 2, 2, 2, 2, 2, 2, 2]
        )
        return network



class nnUNetTrainer_MedNeXt_B_kernel3(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[2, 3, 4, 4, 4, 4, 4, 3, 2],  # Expansion ratio as in Swin Transformers
            kernel_size=3,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[2, 2, 2, 2, 2, 2, 2, 2, 2]
        )
        return network


class nnUNetTrainer_MedNeXt_M_kernel3(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[2, 3, 4, 4, 4, 4, 4, 3, 2],  # Expansion ratio as in Swin Transformers
            kernel_size=3,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[3, 4, 4, 4, 4, 4, 4, 4, 3],
            checkpoint_style='outside_block'
        )
        return network


class nnUNetTrainer_MedNeXt_L_kernel3(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[3, 4, 8, 8, 8, 8, 8, 4, 3],  # Expansion ratio as in Swin Transformers
            # exp_r=[3,4,8,8,8,8,8,4,3],         # Expansion ratio as in Swin Transformers
            kernel_size=3,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            # block_counts = [6,6,6,6,4,2,2,2,2],
            block_counts=[3, 4, 8, 8, 8, 8, 8, 4, 3],
            checkpoint_style='outside_block'
        )
        return network


# Kernels of size 5
class nnUNetTrainer_MedNeXt_S_kernel5(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=2,  # Expansion ratio as in Swin Transformers
            kernel_size=5,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[2, 2, 2, 2, 2, 2, 2, 2, 2]
        )
        return network


class nnUNetTrainer_MedNeXt_S_kernel5_lr_1e_4(nnUNetTrainer_MedNeXt_S_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 1e-4


class nnUNetTrainer_MedNeXt_S_kernel5_lr_25e_5(nnUNetTrainer_MedNeXt_S_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 25e-5


class nnUNetTrainer_MedNeXt_B_kernel5(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[2, 3, 4, 4, 4, 4, 4, 3, 2],  # Expansion ratio as in Swin Transformers
            kernel_size=5,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[2, 2, 2, 2, 2, 2, 2, 2, 2]
        )
        return network


class nnUNetTrainer_MedNeXt_B_kernel5_lr_5e_4(nnUNetTrainer_MedNeXt_B_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 5e-4


class nnUNetTrainer_MedNeXt_B_kernel5_lr_25e_5(nnUNetTrainer_MedNeXt_B_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 25e-5


class nnUNetTrainer_MedNeXt_B_kernel5_lr_1e_4(nnUNetTrainer_MedNeXt_B_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 1e-4


class nnUNetTrainer_MedNeXt_M_kernel5(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[2, 3, 4, 4, 4, 4, 4, 3, 2],  # Expansion ratio as in Swin Transformers
            kernel_size=5,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            block_counts=[3, 4, 4, 4, 4, 4, 4, 4, 3],
            checkpoint_style='outside_block'
        )
        return network


class nnUNetTrainer_MedNeXt_M_kernel5_lr_5e_4(nnUNetTrainer_MedNeXt_M_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 5e-4


class nnUNetTrainer_MedNeXt_M_kernel5_lr_25e_5(nnUNetTrainer_MedNeXt_M_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 25e-5


class nnUNetTrainer_MedNeXt_M_kernel5_lr_1e_4(nnUNetTrainer_MedNeXt_M_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 1e-4


class nnUNetTrainer_MedNeXt_L_kernel5(nnUNetTrainer_Optim_and_LR):

    def build_network_architecture(self, plans_manager: PlansManager,
                                   dataset_json,
                                   configuration_manager: ConfigurationManager,
                                   num_input_channels,
                                   enable_deep_supervision: bool = True):
        network = MedNeXt(
            in_channels=self.num_input_channels,
            n_channels=32,
            n_classes=self.label_manager.num_segmentation_heads,
            exp_r=[3, 4, 8, 8, 8, 8, 8, 4, 3],  # Expansion ratio as in Swin Transformers
            kernel_size=5,  # Can test kernel_size
            deep_supervision=True,  # Can be used to test deep supervision
            do_res=True,  # Can be used to individually test residual connection
            do_res_up_down=True,
            # block_counts = [6,6,6,6,4,2,2,2,2],
            block_counts=[3, 4, 8, 8, 8, 8, 8, 4, 3],
            checkpoint_style='outside_block'
        )
        return network


class nnUNetTrainer_MedNeXt_L_kernel5_lr_5e_4(nnUNetTrainer_MedNeXt_L_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 5e-4


class nnUNetTrainer_MedNeXt_L_kernel5_lr_25e_5(nnUNetTrainer_MedNeXt_L_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 25e-5


class nnUNetTrainer_MedNeXt_L_kernel5_lr_1e_4(nnUNetTrainer_MedNeXt_L_kernel5):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_lr = 1e-4

