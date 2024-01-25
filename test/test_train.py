if __name__ == "__main__":
    import sys, os
    sys.path.append(os.getcwd())

import os
from tqdm import tqdm
from accelerate.utils import ProjectConfiguration, set_seed
from accelerate import Accelerator
from datasets import load_dataset
import torch
from torchvision import transforms

import argparse
import json
def parse_palette_argument(palette_string):
    return json.loads(palette_string)

def parse_args():
    parser = argparse.ArgumentParser(description="diffusion test train")
    parser.add_argument(
        "--tokenizer",
        type=bool,
        default=True
    )
    parser.add_argument(
        "--diffusion_model_path",
        type=str,
        default="",
    )
    parser.add_argument(
        "--controlnet",
        action="store_true",
    )
    parser.add_argument(
        "--controlnet_model_path",
        type=bool,
        default=True
    )
    parser.add_argument(
        "--color_palette_embedding",
        type=bool,
        default=True
    )
    parser.add_argument(
        "--color_palette_embedding_model_path",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--lora",
        type=bool,
        default=True
    )
    parser.add_argument(
        "--train_data_path",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--validation_prompts",
        type=list,
        default=["The cute cat", "The beautiful perfume"]
    )
    parser.add_argument(
        "--validation_palettes",
        type=parse_palette_argument,
        default=[[[45, 36, 32], [162, 169, 177], [76, 85, 92], [144, 120, 103]], [[255, 1, 2], [10, 40, 230], [50, 245, 10], [255, 255, 1]]]
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--save_ckpt_step",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--validation_step",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--precision",
        type=str,
        default="fp16",
        choices=["no", "fp16", "bf16"],
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="wandb",
        choices=["wandb"],
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        choices=["cpu", "cuda"],
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1e-5,
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
    )

    args = parser.parse_args()
    return args

def make_train_dataset(path, tokenizer, accelerator):
    dataset = load_dataset(path)
    column_names = dataset['train'].column_names
    image_column, caption_column, color_column = column_names

    image_transforms = transforms.Compose(
        [
            transforms.Resize(512, interpolation=transforms.InterpolationMode.BILINEAR),
            transforms.CenterCrop(512),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    def preprocess_train(examples):
        images = [image.convert("RGB") for image in examples[image_column]]
        images = [image_transforms(image) for image in images]

        tokenized_ids = tokenizer.batch_encode_plus(examples[caption_column], padding="max_length", max_length=77).input_ids

        colors = torch.FloatTensor([cur["total"] for cur in examples[color_column]])
        
        examples["pixel_values"] = images
        examples["input_ids"] = tokenized_ids
        examples["colors"] = colors

        return examples
    
    with accelerator.main_process_first():
        train_dataset = dataset["train"].with_transform(preprocess_train)
    
    return train_dataset

def load_models(args):
    if args.precision == "fp16":
        precison = torch.float16
    else:
        precison = torch.float32

    tokenizer = None
    if args.tokenizer == True:
        from transformers import CLIPTokenizer
        tokenizer = CLIPTokenizer("./data/vocab.json", merges_file="./data/merges.txt")

    from utils.model_loader import load_diffusion_model
    if args.diffusion_model_path is not None:
        diffusion_state_dict = torch.load(args.diffusion_model_path)

        if "diffusion" not in diffusion_state_dict.keys():
            from utils.model_converter import convert_model
            diffusion_state_dict = convert_model(diffusion_state_dict)
            
    models = load_diffusion_model(diffusion_state_dict, dtype=precison, **{"is_lora":args.lora, "lora_scale":1.0})

    if args.controlnet == True:
        from utils.model_loader import load_controlnet_model
        control_state_dict = None
        if args.controlnet_model_path is not None:
            control_state_dict = torch.load(args.controlnet_model_path)
        
            if "controlnet" not in control_state_dict.keys():
                from utils.model_converter import convert_controlnet_model
                control_state_dict = convert_controlnet_model(control_state_dict)
        controlnet = load_controlnet_model(control_state_dict, dtype=precison)
        models.update(controlnet)

    if args.color_palette_embedding == True:
        from utils.model_loader import load_color_palette_embedding_model
        embedding_state_dict = None
        if args.color_palette_embedding_model_path is not None:
            embedding_state_dict = torch.load(args.color_palette_embedding_model_path)
        embedding = load_color_palette_embedding_model(embedding_state_dict, dtype=torch.float32)
        models.update(embedding)

    return models, tokenizer

import wandb
from utils.color_utils import make_pil_rgb_colors
from pipelines.pipline_color_palette_embedding import generate
from PIL import Image
def log_validation(encoder, decoder, clip, tokenizer, diffusion, embedding, embedding_ts, accelerator, args):

    embedding = accelerator.unwrap_model(embedding)
    embedding_ts = accelerator.unwrap_model(embedding_ts)

    models = {}
    models['clip'] = clip
    models['encoder'] = encoder
    models['decoder'] = decoder
    models['diffusion'] = diffusion
    models['color_palette_embedding'] = embedding
    models['color_palette_timestep_embedding'] = embedding_ts

    image_logs = []
    for validation_prompt, validation_palette in zip(args.validation_prompts, args.validation_palettes):
        for seed in [12345]:
            output_image = generate(
                prompt=validation_prompt,
                uncond_prompt="",
                color_palette=validation_palette,
                do_cfg=True,
                cfg_scale=7.5,
                sampler_name="ddpm",
                n_inference_steps=20,
                strength=1.0,
                models=models,
                seed=seed,
                device=accelerator.device,
                idle_device="cuda",
                tokenizer=tokenizer,
                leave_tqdm=False
            )

            image = Image.fromarray(output_image)

            image_logs.append(
                {"validation_palettes": validation_palette, "images": image, "validation_prompts": validation_prompt}
            )

    for tracker in accelerator.trackers:
        if tracker.name == "wandb":
            formatted_images = []

            for log in image_logs:
                image = log["images"]
                validation_prompt = log["validation_prompts"]
                validation_palette = log["validation_palettes"]

                validation_palette_pil = make_pil_rgb_colors(validation_palette).resize([512,512])
                formatted_images.append(wandb.Image(validation_palette_pil, caption="Palette conditioning"))
                formatted_images.append(wandb.Image(image, caption=validation_prompt))

            tracker.log({"validation": formatted_images})

    return image_logs

def collate_fn(examples):
    pixel_values = torch.stack([example["pixel_values"] for example in examples])
    pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

    input_ids = torch.tensor([example["input_ids"] for example in examples], dtype=torch.long)
    
    colors = torch.stack([example["colors"] for example in examples])
    colors = colors.to(memory_format=torch.contiguous_format).float()

    return {
        "pixel_values": pixel_values,
        "input_ids": input_ids,
        "colors": colors,
    }

def get_time_embedding(timestep, dtype=torch.float16):
    freqs = torch.pow(10000, -torch.arange(start=0, end=160, dtype=dtype) / 160) 
    x = torch.tensor(timestep, dtype=dtype)[:, None] * freqs[None]
    return torch.cat([torch.cos(x), torch.sin(x)], dim=-1)


import torch.nn.functional as F
def train(accelerator,
        train_dataloader,
        tokenizer,
        clip,
        encoder,
        decoder,
        diffusion,
        embedding,
        embedding_ts,
        lora_wrapper_model,
        sampler,
        optimizer,
        lr_scheduler,
        weight_dtype,
        args):
    
    global_step = 0
    progress_bar = tqdm(
        range(0, args.epochs * len(train_dataloader)),
        initial=global_step,
        desc="Steps",
        disable=not accelerator.is_local_main_process,
    )

    for epoch in range(args.epochs):
        for step, batch in enumerate(train_dataloader):
            latents = encoder(batch["pixel_values"].to(dtype=weight_dtype))

            noise = torch.randn_like(latents)
            batch_size = batch['pixel_values'].shape[0]
            
            timesteps = torch.randint(0, sampler.num_train_timesteps, (batch_size,), device="cpu").long()
            
            latents = sampler.add_noise(latents, timesteps, noise)
            
            contexts = clip(batch['input_ids'])
            colors = batch["colors"]
            colorpalette_model = embedding
            context_cat = colorpalette_model(colors).to("cuda")
            contexts = torch.cat([contexts, context_cat], 1).to(dtype=weight_dtype)

            time_embeddings = get_time_embedding(timesteps).to(latents.device)
            colorpalette_ts_model = embedding_ts
            time_sum = colorpalette_ts_model(colors).to(dtype=weight_dtype)
            time_embeddings += time_sum

            model_pred = diffusion(
                latents,
                contexts,
                time_embeddings
            )

            target = noise
            loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

            accelerator.backward(loss)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad(set_to_none=False)


            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1

                if accelerator.is_main_process:

                    if global_step % args.save_ckpt_step == 0:
                        save_path = os.path.join("./training", f"checkpoint-{global_step}")
                        os.makedirs(save_path,exist_ok=True)

                        embedding = accelerator.unwrap_model(embedding)
                        embedding_ts = accelerator.unwrap_model(embedding_ts)
                        lora_wrapper_model = accelerator.unwrap_model(lora_wrapper_model)

                        torch.save(embedding, f"./training/embedding_{epoch}.pth")
                        torch.save(embedding_ts, f"./training/embeddingts_{epoch}.pth")
                        torch.save(embedding_ts, f"./training/lora_{epoch}.pth")
                    
                    if global_step % args.validation_step == 0:
                        log_validation(encoder,
                                    decoder,
                                    clip,
                                    tokenizer,
                                    diffusion,
                                    embedding,
                                    embedding_ts,
                                    accelerator,
                                    args)
                        
                        lora_wrapper_model = accelerator.unwrap_model(lora_wrapper_model)

            logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)
            accelerator.log(logs, step=global_step)
        
        accelerator.wait_for_everyone()
        if accelerator.is_main_process:
            embedding = accelerator.unwrap_model(embedding)
            embedding_ts = accelerator.unwrap_model(embedding_ts)
            lora_wrapper_model = accelerator.unwrap_model(lora_wrapper_model)

            torch.save(embedding, f"./training/embedding_{epoch}.pth")
            torch.save(embedding_ts, f"./training/embeddingts_{epoch}.pth")
            torch.save(embedding_ts, f"./training/lora_{epoch}.pth")

def main(args):
    cur_dir = os.path.dirname(os.path.abspath(__name__))
    os.makedirs(os.path.join(cur_dir, "training"), exist_ok=True)
    os.makedirs(os.path.join(cur_dir, "training", "log"), exist_ok=True)

    accelerator_project_config = ProjectConfiguration(
        project_dir=os.path.join(cur_dir, "training"),
        logging_dir=os.path.join(cur_dir, "training", "log")
    )

    accelerator = Accelerator(
        gradient_accumulation_steps=1,
        mixed_precision=args.precision,
        log_with= args.report_to,
        project_config=accelerator_project_config
    )

    generator = torch.Generator(device=args.device)

    if args.seed is not None:
        set_seed(args.seed)
        generator.manual_seed(42)
    else:
        set_seed(42)
        generator.manual_seed(42)

    models, tokenizer = load_models(args)

    clip = models['clip']
    encoder = models['encoder']
    decoder = models['decoder'] 
    diffusion = models['diffusion']
    embedding = models['color_palette_embedding']
    embedding_ts = models['color_palette_timestep_embedding']

    from models.lora.lora import extract_lora_from_unet
    lora_wrapper_model = extract_lora_from_unet(diffusion)

    clip.requires_grad_(False)
    encoder.requires_grad_(False)
    decoder.requires_grad_(False)

    embedding.train()
    embedding_ts.train()
    lora_wrapper_model.train()

    train_dataset = make_train_dataset(args.train_data_path, tokenizer, accelerator)

    from torch.utils.data import DataLoader
    train_dataloader = DataLoader(
        train_dataset, 
        shuffle=True, 
        collate_fn=collate_fn,
        batch_size=2,
        num_workers=0
    )
 
    from torch.optim import AdamW
    params_to_optimize = list(embedding.parameters()) + list(embedding_ts.parameters()) + list(lora_wrapper_model.parameters())
    optimizer = AdamW(
            params_to_optimize,
            lr=args.lr,
            betas=(0.9, 0.999),
            weight_decay=1e-2,
            eps=1e-08,
        )

    from torch.optim.lr_scheduler import LambdaLR
    lr_scheduler = LambdaLR(optimizer, lambda _: 1, last_epoch=-1)

    from models.scheduler.ddpm import DDPMSampler
    sampler = DDPMSampler(generator)
    
    lora_wrapper_model, embedding, embedding_ts, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        lora_wrapper_model, embedding, embedding_ts, optimizer, train_dataloader, lr_scheduler
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    if accelerator.is_main_process:
        tracker_config = dict(vars(args))

        tracker_config.pop("validation_prompts")
        tracker_config.pop("validation_palettes")

        accelerator.init_trackers("train_color_palette_embedding", config=tracker_config)
    
    clip.to(accelerator.device, dtype=weight_dtype)
    encoder.to(accelerator.device, dtype=weight_dtype)
    decoder.to(accelerator.device, dtype=weight_dtype)
    diffusion.to(accelerator.device, dtype=weight_dtype)
    lora_wrapper_model.to(accelerator.device, dtype=torch.float32)
    
    train(accelerator,
        train_dataloader,
        tokenizer,
        clip,
        encoder,
        decoder,
        diffusion,
        embedding,
        embedding_ts,
        lora_wrapper_model,
        sampler,
        optimizer,
        lr_scheduler,
        weight_dtype,
        args)

    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
