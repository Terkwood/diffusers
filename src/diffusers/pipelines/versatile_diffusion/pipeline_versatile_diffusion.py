# Copyright 2022 The HuggingFace Team. All rights reserved.
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

import inspect
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.utils.checkpoint

from transformers import CLIPProcessor, CLIPTextModel, CLIPTokenizer, CLIPVisionModel

from ...models import AutoencoderKL, UNet2DConditionModel, UNet2DModel, VQModel
from ...pipeline_utils import DiffusionPipeline, ImagePipelineOutput
from ...schedulers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler


class VersatileDiffusionPipeline(DiffusionPipeline):
    r"""
    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Parameters:
        vqvae ([`VQModel`]):
            Vector-quantized (VQ) Model to encode and decode images to and from latent representations.
        bert ([`LDMBertModel`]):
            Text-encoder model based on [BERT](https://huggingface.co/docs/transformers/model_doc/bert) architecture.
        tokenizer (`transformers.BertTokenizer`):
            Tokenizer of class
            [BertTokenizer](https://huggingface.co/docs/transformers/model_doc/bert#transformers.BertTokenizer).
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
    """
    tokenizer: CLIPTokenizer
    image_processor: CLIPProcessor
    text_encoder: CLIPTextModel
    image_encoder: CLIPVisionModel
    image_unet: UNet2DConditionModel
    vae: Union[VQModel, AutoencoderKL]
    scheduler: Union[DDIMScheduler, PNDMScheduler, LMSDiscreteScheduler]

    def __init__(
        self,
        tokenizer: CLIPTokenizer,
        image_processor: CLIPProcessor,
        text_encoder: CLIPTextModel,
        image_encoder: CLIPVisionModel,
        image_unet: UNet2DConditionModel,
        vae: Union[VQModel, AutoencoderKL],
        scheduler: Union[DDIMScheduler, PNDMScheduler, LMSDiscreteScheduler],
    ):
        super().__init__()
        self.register_modules(
            tokenizer=tokenizer,
            image_processor=image_processor,
            text_encoder=text_encoder,
            image_encoder=image_encoder,
            image_unet=image_unet,
            vae=vae,
            scheduler=scheduler,
        )

    def _encode_prompt(self, prompt, do_classifier_free_guidance):
        r"""
        Encodes the prompt into text encoder hidden states.

        Args:
            prompt (`str` or `list(int)`):
                prompt to be encoded
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
        """

        def _normalize_embeddings(encoder_output):
            embeds = self.text_encoder.text_projection(encoder_output.last_hidden_state)
            embeds_pooled = encoder_output.text_embeds
            embeds = embeds / torch.norm(embeds_pooled.unsqueeze(1), dim=-1, keepdim=True)
            return embeds

        batch_size = len(prompt) if isinstance(prompt, list) else 1

        if do_classifier_free_guidance:
            uncond_input = self.tokenizer([""] * batch_size, padding="max_length", max_length=77, return_tensors="pt")
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))
            uncond_embeddings = _normalize_embeddings(uncond_embeddings)

        # get prompt text embeddings
        text_input = self.tokenizer(prompt, padding="max_length", max_length=77, return_tensors="pt")
        text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))
        text_embeddings = _normalize_embeddings(text_embeddings)

        # For classifier free guidance, we need to do two forward passes.
        # Here we concatenate the unconditional and text embeddings into a single batch
        # to avoid doing two forward passes
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        return text_embeddings

    @torch.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 1.0,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        **kwargs,
    ) -> Union[Tuple, ImagePipelineOutput]:
        r"""
        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            height (`int`, *optional*, defaults to 256):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 256):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 1.0):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt` at
                the, usually at the expense of lower image quality.
            generator (`torch.Generator`, *optional*):
                A [torch generator](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make generation
                deterministic.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*):
                Whether or not to return a [`~pipeline_utils.ImagePipelineOutput`] instead of a plain tuple.

        Returns:
            [`~pipeline_utils.ImagePipelineOutput`] or `tuple`: [`~pipelines.utils.ImagePipelineOutput`] if
            `return_dict` is True, otherwise a `tuple. When returning a tuple, the first element is a list with the
            generated images.
        """

        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        do_classifier_free_guidance = guidance_scale > 1.0

        text_embeddings = self._encode_prompt(prompt, do_classifier_free_guidance)

        latents = torch.randn(
            (batch_size, self.image_unet.in_channels, height // 8, width // 8),
            generator=generator,
        )
        latents = latents.to(self.device)

        self.scheduler.set_timesteps(num_inference_steps)

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())

        extra_kwargs = {}
        if accepts_eta:
            extra_kwargs["eta"] = eta

        for t in self.progress_bar(self.scheduler.timesteps):
            if not do_classifier_free_guidance:
                latents_input = latents
            else:
                latents_input = torch.cat([latents] * 2)

            # predict the noise residual
            noise_pred = self.image_unet(latents_input, t, encoder_hidden_states=text_embeddings).sample
            # perform guidance
            if guidance_scale != 1.0:
                noise_pred_uncond, noise_prediction_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_prediction_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.scheduler.step(noise_pred, t, latents, **extra_kwargs).prev_sample

        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()
        if output_type == "pil":
            image = self.numpy_to_pil(image)

        if not return_dict:
            return (image,)

        return ImagePipelineOutput(images=image)