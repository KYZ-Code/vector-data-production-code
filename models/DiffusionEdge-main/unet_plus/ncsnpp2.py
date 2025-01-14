# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: skip-file

from unet_plus import utils, layers, layerspp, normalization
import torch.nn as nn
import functools
import torch
import numpy as np

ResnetBlockDDPM = layerspp.ResnetBlockDDPMpp
ResnetBlockBigGAN = layerspp.ResnetBlockBigGANpp
Combine = layerspp.Combine
conv3x3 = layerspp.conv3x3
conv1x1 = layerspp.conv1x1
get_act = layers.get_act
get_normalization = normalization.get_normalization
default_initializer = layers.default_init


@utils.register_model(name='ncsnpp')
class NCSNpp(nn.Module):
  """NCSN++ model"""

  def __init__(self, config):
    super().__init__()
    self.config = config
    self.act = act = get_act(config)
    self.register_buffer('sigmas', torch.tensor(utils.get_sigmas(config)))

    self.nf = nf = config.nf
    ch_mult = config.ch_mult
    self.num_res_blocks = num_res_blocks = config.num_res_blocks
    self.attn_resolutions = attn_resolutions = config.attn_resolutions
    dropout = config.dropout
    resamp_with_conv = config.resamp_with_conv
    self.num_resolutions = num_resolutions = len(ch_mult)
    self.all_resolutions = all_resolutions = [config.image_size // (2 ** i) for i in range(num_resolutions)]

    self.conditional = conditional = config.conditional  # noise-conditional
    fir = config.fir
    fir_kernel = config.fir_kernel
    self.skip_rescale = skip_rescale = config.skip_rescale
    self.resblock_type = resblock_type = config.resblock_type.lower()
    self.progressive = progressive = config.progressive.lower()
    self.progressive_input = progressive_input = config.progressive_input.lower()
    self.embedding_type = embedding_type = config.embedding_type.lower()
    init_scale = config.init_scale
    assert progressive in ['none', 'output_skip', 'residual']
    assert progressive_input in ['none', 'input_skip', 'residual']
    assert embedding_type in ['fourier', 'positional']
    combine_method = config.progressive_combine.lower()
    combiner = functools.partial(Combine, method=combine_method)

    modules = []
    # timestep/noise_level embedding; only for continuous training
    if embedding_type == 'fourier':
      # Gaussian Fourier features embeddings.
      assert config.continuous, "Fourier features are only used for continuous training."

      modules.append(layerspp.GaussianFourierProjection(
        embedding_size=nf, scale=config.fourier_scale
      ))
      embed_dim = 2 * nf

    elif embedding_type == 'positional':
      embed_dim = nf

    else:
      raise ValueError(f'embedding type {embedding_type} unknown.')

    if conditional:
      modules.append(nn.Linear(embed_dim, nf * 4))
      modules[-1].weight.data = default_initializer()(modules[-1].weight.shape)
      nn.init.zeros_(modules[-1].bias)
      modules.append(nn.Linear(nf * 4, nf * 4))
      modules[-1].weight.data = default_initializer()(modules[-1].weight.shape)
      nn.init.zeros_(modules[-1].bias)
    # if self.config.scale_by_sigma:
    #   self.sigma_layer = nn.Linear(nf * 4, config.in_channels * config.out_mul)
    #   # self.sigma_layer.weight.data = default_initializer()(self.sigma_layer.weight.shape)
    #   self.sigma_layer.weight.data.fill_(1)
    #   nn.init.zeros_(self.sigma_layer.bias)

    AttnBlock = functools.partial(layerspp.AttnBlockpp,
                                  init_scale=init_scale,
                                  skip_rescale=skip_rescale)

    Upsample = functools.partial(layerspp.Upsample,
                                 with_conv=resamp_with_conv, fir=fir, fir_kernel=fir_kernel)

    if progressive == 'output_skip':
      self.pyramid_upsample = layerspp.Upsample(fir=fir, fir_kernel=fir_kernel, with_conv=False)
    elif progressive == 'residual':
      pyramid_upsample = functools.partial(layerspp.Upsample,
                                           fir=fir, fir_kernel=fir_kernel, with_conv=True)

    Downsample = functools.partial(layerspp.Downsample,
                                   with_conv=resamp_with_conv, fir=fir, fir_kernel=fir_kernel)

    if progressive_input == 'input_skip':
      self.pyramid_downsample = layerspp.Downsample(fir=fir, fir_kernel=fir_kernel, with_conv=False)
    elif progressive_input == 'residual':
      pyramid_downsample = functools.partial(layerspp.Downsample,
                                             fir=fir, fir_kernel=fir_kernel, with_conv=True)

    if resblock_type == 'ddpm':
      ResnetBlock = functools.partial(ResnetBlockDDPM,
                                      act=act,
                                      dropout=dropout,
                                      init_scale=init_scale,
                                      skip_rescale=skip_rescale,
                                      temb_dim=nf * 4)

    elif resblock_type == 'biggan':
      ResnetBlock = functools.partial(ResnetBlockBigGAN,
                                      act=act,
                                      dropout=dropout,
                                      fir=fir,
                                      fir_kernel=fir_kernel,
                                      init_scale=init_scale,
                                      skip_rescale=skip_rescale,
                                      temb_dim=nf * 4)

    else:
      raise ValueError(f'resblock type {resblock_type} unrecognized.')

    # Downsampling block

    channels = config.in_channels
    self.channels = channels
    self.self_condition = False
    if progressive_input != 'none':
      input_pyramid_ch = channels

    modules.append(conv3x3(channels, nf))
    hs_c = [nf]
    hs_c2 = [nf]

    in_ch = nf
    for i_level in range(num_resolutions):
      # Residual blocks for this resolution
      for i_block in range(num_res_blocks):
        out_ch = nf * ch_mult[i_level]
        modules.append(ResnetBlock(in_ch=in_ch, out_ch=out_ch))
        in_ch = out_ch

        if all_resolutions[i_level] in attn_resolutions:
          modules.append(AttnBlock(channels=in_ch))
        hs_c.append(in_ch)
        hs_c2.append(in_ch)

      if i_level != num_resolutions - 1:
        if resblock_type == 'ddpm':
          modules.append(Downsample(in_ch=in_ch))
        else:
          modules.append(ResnetBlock(down=True, in_ch=in_ch))

        if progressive_input == 'input_skip':
          modules.append(combiner(dim1=input_pyramid_ch, dim2=in_ch))
          if combine_method == 'cat':
            in_ch *= 2

        elif progressive_input == 'residual':
          modules.append(pyramid_downsample(in_ch=input_pyramid_ch, out_ch=in_ch))
          input_pyramid_ch = in_ch

        hs_c.append(in_ch)
        hs_c2.append(in_ch)
    # hs_c2 = hs_c
    in_ch = hs_c[-1]
    modules.append(ResnetBlock(in_ch=in_ch))
    modules.append(AttnBlock(channels=in_ch))
    modules.append(ResnetBlock(in_ch=in_ch))
    modules.append(nn.Conv2d(in_ch, in_ch, 1))
    modules.append(
      nn.Sequential(
      nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6),
      nn.SiLU(),
      conv3x3(in_ch, channels*(config.out_mul-1), init_scale=init_scale)
    ))
    modules.append(nn.Conv2d(in_ch, in_ch, 1))
    modules.append(
      nn.Sequential(
      nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6),
      nn.SiLU(),
      conv3x3(in_ch, channels, init_scale=init_scale)
    ))

    pyramid_ch = 0
    # Upsampling block
    for i_level in reversed(range(num_resolutions)):
      for i_block in range(num_res_blocks + 1):
        out_ch = nf * ch_mult[i_level]
        modules.append(ResnetBlock(in_ch=in_ch + hs_c.pop(),
                                   out_ch=out_ch))
        in_ch = out_ch

      if all_resolutions[i_level] in attn_resolutions:
        modules.append(AttnBlock(channels=in_ch))

      if progressive != 'none':
        if i_level == num_resolutions - 1:
          if progressive == 'output_skip':
            modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            modules.append(conv3x3(in_ch, channels, init_scale=init_scale))
            pyramid_ch = channels
          elif progressive == 'residual':
            modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            modules.append(conv3x3(in_ch, in_ch, bias=True))
            pyramid_ch = in_ch
          else:
            raise ValueError(f'{progressive} is not a valid name.')
        else:
          if progressive == 'output_skip':
            modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            modules.append(conv3x3(in_ch, channels, bias=True, init_scale=init_scale))
            pyramid_ch = channels
          elif progressive == 'residual':
            modules.append(pyramid_upsample(in_ch=pyramid_ch, out_ch=in_ch))
            pyramid_ch = in_ch
          else:
            raise ValueError(f'{progressive} is not a valid name')

      if i_level != 0:
        if resblock_type == 'ddpm':
          modules.append(Upsample(in_ch=in_ch))
        else:
          modules.append(ResnetBlock(in_ch=in_ch, up=True))

    assert not hs_c
    if progressive != 'output_skip':
      modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                  num_channels=in_ch, eps=1e-6))
      modules.append(conv3x3(in_ch, channels*(config.out_mul-1), init_scale=init_scale))
    self.out_mul = config.out_mul

    self.all_modules = nn.ModuleList(modules)

    additional_modules = nn.ModuleList()
    pyramid_ch = 0
    in_ch = hs_c2[-1]
    # Upsampling block
    for i_level in reversed(range(num_resolutions)):
      for i_block in range(num_res_blocks + 1):
        out_ch = nf * ch_mult[i_level]
        additional_modules.append(ResnetBlock(in_ch=in_ch + hs_c2.pop(),
                                   out_ch=out_ch))
        in_ch = out_ch

      if all_resolutions[i_level] in attn_resolutions:
        additional_modules.append(AttnBlock(channels=in_ch))

      if progressive != 'none':
        if i_level == num_resolutions - 1:
          if progressive == 'output_skip':
            additional_modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            additional_modules.append(conv3x3(in_ch, channels, init_scale=init_scale))
            pyramid_ch = channels
          elif progressive == 'residual':
            additional_modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            additional_modules.append(conv3x3(in_ch, in_ch, bias=True))
            pyramid_ch = in_ch
          else:
            raise ValueError(f'{progressive} is not a valid name.')
        else:
          if progressive == 'output_skip':
            additional_modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                        num_channels=in_ch, eps=1e-6))
            additional_modules.append(conv3x3(in_ch, channels, bias=True, init_scale=init_scale))
            pyramid_ch = channels
          elif progressive == 'residual':
            additional_modules.append(pyramid_upsample(in_ch=pyramid_ch, out_ch=in_ch))
            pyramid_ch = in_ch
          else:
            raise ValueError(f'{progressive} is not a valid name')

      if i_level != 0:
        if resblock_type == 'ddpm':
          additional_modules.append(Upsample(in_ch=in_ch))
        else:
          additional_modules.append(ResnetBlock(in_ch=in_ch, up=True))
    assert not hs_c2
    if progressive != 'output_skip':
      additional_modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32),
                                  num_channels=in_ch, eps=1e-6))
      additional_modules.append(conv3x3(in_ch, channels, init_scale=init_scale))
    self.additional_modules = additional_modules

  def forward(self, x, time_cond):
    # timestep/noise_level embedding; only for continuous training
    modules = self.all_modules
    m_idx = 0
    if self.embedding_type == 'fourier':
      # Gaussian Fourier features embeddings.
      used_sigmas = time_cond
      temb = modules[m_idx](torch.log(used_sigmas))
      m_idx += 1

    elif self.embedding_type == 'positional':
      # Sinusoidal positional embeddings.
      timesteps = time_cond
      used_sigmas = self.sigmas[time_cond.long()]
      temb = layers.get_timestep_embedding(timesteps, self.nf)

    else:
      raise ValueError(f'embedding type {self.embedding_type} unknown.')

    if self.conditional:
      temb = modules[m_idx](temb)
      m_idx += 1
      temb = modules[m_idx](self.act(temb))
      m_idx += 1
    else:
      temb = None

    # if not self.config.data.centered:
    #   # If input data is in [0, 1]
    #   x = 2 * x - 1.

    # Downsampling block
    input_pyramid = None
    if self.progressive_input != 'none':
      input_pyramid = x

    tmp = modules[m_idx](x)
    hs = [tmp]
    hs2 = [tmp]
    m_idx += 1
    for i_level in range(self.num_resolutions):
      # Residual blocks for this resolution
      for i_block in range(self.num_res_blocks):
        h = modules[m_idx](hs[-1], temb)
        m_idx += 1
        if h.shape[-1] in self.attn_resolutions:
          h = modules[m_idx](h)
          m_idx += 1

        hs.append(h)
        hs2.append(h)

      if i_level != self.num_resolutions - 1:
        if self.resblock_type == 'ddpm':
          h = modules[m_idx](hs[-1])
          m_idx += 1
        else:
          h = modules[m_idx](hs[-1], temb)
          m_idx += 1

        if self.progressive_input == 'input_skip':
          input_pyramid = self.pyramid_downsample(input_pyramid)
          h = modules[m_idx](input_pyramid, h)
          m_idx += 1

        elif self.progressive_input == 'residual':
          input_pyramid = modules[m_idx](input_pyramid)
          m_idx += 1
          if self.skip_rescale:
            input_pyramid = (input_pyramid + h) / np.sqrt(2.)
          else:
            input_pyramid = input_pyramid + h
          h = input_pyramid

        hs.append(h)
        hs2.append(h)
    # hs2 = hs
    h = hs[-1]
    h = modules[m_idx](h, temb)
    m_idx += 1
    h = modules[m_idx](h)
    m_idx += 1
    h = modules[m_idx](h, temb)
    m_idx += 1

    hf1 = modules[m_idx](h)
    m_idx += 1
    pred_C_temp = modules[m_idx](hf1)
    m_idx += 1
    hf2 = modules[m_idx](h)
    m_idx += 1
    pred_noise_temp = modules[m_idx](hf2)
    m_idx += 1

    pyramid = None

    # Upsampling block
    for i_level in reversed(range(self.num_resolutions)):
      for i_block in range(self.num_res_blocks + 1):
        hf1 = modules[m_idx](torch.cat([hf1, hs.pop()], dim=1), temb)
        m_idx += 1

      if hf1.shape[-1] in self.attn_resolutions:
        hf1 = modules[m_idx](hf1)
        m_idx += 1

      if self.progressive != 'none':
        if i_level == self.num_resolutions - 1:
          if self.progressive == 'output_skip':
            pyramid = self.act(modules[m_idx](hf1))
            m_idx += 1
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
          elif self.progressive == 'residual':
            pyramid = self.act(modules[m_idx](hf1))
            m_idx += 1
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
          else:
            raise ValueError(f'{self.progressive} is not a valid name.')
        else:
          if self.progressive == 'output_skip':
            pyramid = self.pyramid_upsample(pyramid)
            pyramid_h = self.act(modules[m_idx](hf1))
            m_idx += 1
            pyramid_h = modules[m_idx](pyramid_h)
            m_idx += 1
            pyramid = pyramid + pyramid_h
          elif self.progressive == 'residual':
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
            if self.skip_rescale:
              pyramid = (pyramid + hf1) / np.sqrt(2.)
            else:
              pyramid = pyramid + hf1
            hf1 = pyramid
          else:
            raise ValueError(f'{self.progressive} is not a valid name')

      if i_level != 0:
        if self.resblock_type == 'ddpm':
          hf1 = modules[m_idx](hf1)
          m_idx += 1
        else:
          hf1 = modules[m_idx](hf1, temb)
          m_idx += 1

    assert not hs

    if self.progressive == 'output_skip':
      hf1 = pyramid
    else:
      hf1 = self.act(modules[m_idx](hf1))
      m_idx += 1
      hf1 = modules[m_idx](hf1)
      m_idx += 1

    assert m_idx == len(modules)
    pred_C = hf1

    ### another branch
    m_idx = 0
    modules = self.additional_modules
    pyramid = None
    # Upsampling block
    for i_level in reversed(range(self.num_resolutions)):
      for i_block in range(self.num_res_blocks + 1):
        hf2 = modules[m_idx](torch.cat([hf2, hs2.pop()], dim=1), temb)
        m_idx += 1

      if hf2.shape[-1] in self.attn_resolutions:
        hf2 = modules[m_idx](hf2)
        m_idx += 1

      if self.progressive != 'none':
        if i_level == self.num_resolutions - 1:
          if self.progressive == 'output_skip':
            pyramid = self.act(modules[m_idx](hf2))
            m_idx += 1
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
          elif self.progressive == 'residual':
            pyramid = self.act(modules[m_idx](hf2))
            m_idx += 1
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
          else:
            raise ValueError(f'{self.progressive} is not a valid name.')
        else:
          if self.progressive == 'output_skip':
            pyramid = self.pyramid_upsample(pyramid)
            pyramid_h = self.act(modules[m_idx](hf2))
            m_idx += 1
            pyramid_h = modules[m_idx](pyramid_h)
            m_idx += 1
            pyramid = pyramid + pyramid_h
          elif self.progressive == 'residual':
            pyramid = modules[m_idx](pyramid)
            m_idx += 1
            if self.skip_rescale:
              pyramid = (pyramid + hf2) / np.sqrt(2.)
            else:
              pyramid = pyramid + hf2
            hf2 = pyramid
          else:
            raise ValueError(f'{self.progressive} is not a valid name')

      if i_level != 0:
        if self.resblock_type == 'ddpm':
          hf2 = modules[m_idx](hf2)
          m_idx += 1
        else:
          hf2 = modules[m_idx](hf2, temb)
          m_idx += 1

    assert not hs

    if self.progressive == 'output_skip':
      hf2 = pyramid
    else:
      hf2 = self.act(modules[m_idx](hf2))
      m_idx += 1
      hf2 = modules[m_idx](hf2)
      m_idx += 1

    assert m_idx == len(modules)
    pred_noise = hf2
    return pred_C, pred_noise, pred_C_temp, pred_noise_temp

if __name__ == '__main__':
  import yaml
  from fvcore.common.config import CfgNode
  with open('../configs/cifar10/uncond_etp_const_dpm_sde.yaml') as f:
    exp_conf = yaml.load(f, Loader=yaml.FullLoader)
  config = CfgNode(exp_conf)
  config = config.model.ncsnpp
  model = NCSNpp(config)
  x = torch.rand(2, 3, 32, 32)
  eps = 1e-4
  t = torch.rand(x.shape[0], device=x.device) * (1 - eps) + eps
  y = model(x, t)
  pass