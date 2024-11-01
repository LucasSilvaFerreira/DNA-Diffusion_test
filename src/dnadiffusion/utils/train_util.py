import copy
from typing import Any
from pathlib import Path

import torch
import torchvision.transforms as T
from accelerate import Accelerator
from torch.optim import Adam
from torch.utils.data import DataLoader
from tqdm import tqdm

from dnadiffusion.data.dataloader import SequenceDataset
from dnadiffusion.metrics.metrics import compare_motif_list, generate_similarity_using_train
from dnadiffusion.utils.sample_util import create_sample
from dnadiffusion.utils.utils import EMA


class TrainLoop:
    def __init__(
        self,
        data: dict[str, Any],
        model: torch.nn.Module,
        accelerator: Accelerator,
        epochs: int = 10000,
        log_step_show: int = 50,
        sample_epoch: int = 500,
        save_epoch: int = 500,
        model_name: str = "model_48k_sequences_per_group_K562_hESCT0_HepG2_GM12878_12k",
        image_size: int = 200,
        num_sampling_to_compare_cells: int = 1000,
        batch_size: int = 960,
        metric_function=None, #how to type a function
        learning_rate: float = 1e-4, 
        selective_sampling_number :int = None,
        save_lora_function = None,
        lora_path : str = '', 
        lora_save_epoch:  int = 50,
    ):
        self.encode_data = data
        self.learning_rate=learning_rate
        self.model = model
        self.optimizer = Adam(self.model.parameters(), lr=self.learning_rate)
        self.accelerator = accelerator
        self.epochs = epochs
        self.log_step_show = log_step_show
        self.sample_epoch = sample_epoch
        self.save_epoch = save_epoch
        self.model_name = model_name
        self.image_size = image_size
        self.num_sampling_to_compare_cells = num_sampling_to_compare_cells
        self.metric_function=metric_function
        self.selective_sampling_number = selective_sampling_number

        self.save_lora_function = save_lora_function 
        self.lora_path = lora_path
        self.lora_save_epoch = lora_save_epoch 



        if self.accelerator.is_main_process:
            self.ema = EMA(0.995)
            self.ema_model = copy.deepcopy(self.model).eval().requires_grad_(False)

        # Metrics
        self.train_kl, self.test_kl, self.shuffle_kl = 1, 1, 1
        self.seq_similarity = 1

        self.start_epoch = 1

        # Dataloader
        seq_dataset = SequenceDataset(seqs=self.encode_data["X_train"], c=self.encode_data["x_train_cell_type"])
        self.train_dl = DataLoader(seq_dataset, batch_size=batch_size, shuffle=True, num_workers=8, pin_memory=True)

    def train_loop(self):
        # Prepare for training
        self.model, self.optimizer, self.train_dl = self.accelerator.prepare(self.model, self.optimizer, self.train_dl)

        # Initialize wandb
        if self.accelerator.is_main_process:
            self.accelerator.init_trackers(
                "dnadiffusion",
                init_kwargs={"wandb": {"notes": "testing wandb accelerate script"}},
            )

        for epoch in tqdm(range(self.start_epoch, self.epochs + 1)):
            self.model.train()

            # Getting loss of current batch
            for step, batch in enumerate(self.train_dl):
                self.global_step = epoch * len(self.train_dl) + step

                loss = self.train_step(batch)

                # Logging loss
                if self.global_step % self.log_step_show == 0 and self.accelerator.is_main_process:
                    self.log_step(loss, epoch)

            # Sampling
            if epoch % self.sample_epoch == 0 and self.accelerator.is_main_process:
                self.sample()

            # Saving model
            if epoch % self.save_epoch == 0 and self.accelerator.is_main_process:
                self.save_model(epoch)


            # Saving Lora
            if self.save_lora_function and epoch % self.lora_save_epoch == 0 and self.accelerator.is_main_process:
                self.save_lora_model(epoch)

    def train_step(self, batch):
        x, y = batch

        with self.accelerator.autocast():
            loss = self.model(x, y)

        self.optimizer.zero_grad()
        self.accelerator.backward(loss)
        self.accelerator.wait_for_everyone()
        self.optimizer.step()

        self.accelerator.wait_for_everyone()
        if self.accelerator.is_main_process:
            self.ema.step_ema(self.ema_model, self.accelerator.unwrap_model(self.model))

        self.accelerator.wait_for_everyone()
        return loss

    def log_step(self, loss, epoch):
        if self.accelerator.is_main_process:
            self.accelerator.log(
                {
                    "train": self.train_kl,
                    "test": self.test_kl,
                    "shuffle": self.shuffle_kl,
                    "loss": loss.mean().item(),
                    "epoch": epoch,
                    "seq_similarity": self.seq_similarity,
                },
                step=self.global_step,
            )


    def sample(self):
        self.model.eval()

        # Sample from the model
        print("saving")
        print ('encoded data', self.encode_data["cell_types"])
        print (self.encode_data["numeric_to_tag"])
        print ('sampling only from cell number:', self.selective_sampling_number)
        synt_df = create_sample(
            self.accelerator.unwrap_model(self.model),
            conditional_numeric_to_tag=self.encode_data["numeric_to_tag"],
            cell_types=self.encode_data["cell_types"],
            group_number= self.selective_sampling_number,
            number_of_samples=int(self.num_sampling_to_compare_cells / 10),
            cond_weight_to_metric=1

        )
        print(synt_df)
        if self.metric_function != None:
          self.metric_function(input_fasta="synthetic_motifs.fasta")
        #add metrics as a function instead to force it
        #may train loop can receive a function
        # self.seq_similarity = generate_similarity_using_train(self.encode_data["X_train"])
        # self.train_kl = compare_motif_list(synt_df, self.encode_data["train_motifs"])
        # self.test_kl = compare_motif_list(synt_df, self.encode_data["test_motifs"])
        # self.shuffle_kl = compare_motif_list(synt_df, self.encode_data["shuffle_motifs"])
        # print("Similarity", self.seq_similarity, "Similarity")
        # print("KL_TRAIN", self.train_kl, "KL")
        # print("KL_TEST", self.test_kl, "KL")
        # print("KL_SHUFFLE", self.shuffle_kl, "KL")

    def save_model(self, epoch):
        checkpoint_dict = {
            "model": self.accelerator.get_state_dict(self.model),
            "optimizer": self.optimizer.state_dict(),
            "epoch": epoch,
            "ema_model": self.accelerator.get_state_dict(self.ema_model),
        }
        torch.save(
            checkpoint_dict,
            f"epoch_{epoch}_{self.model_name}.pt",
        )


    def save_lora_model(self, epoch):
      path_use =  self.lora_path 
      if self.lora_path == '': 
        path_use = '.'
      Path(path_use).mkdir(parents=True, exist_ok=True)
      final_path_name_and_file = path_use + f'/{str(epoch)}_filepath.pth'
      self.save_lora_function(self.model.model,  final_path_name_and_file.replace('//','/'))
      print (f'Lora saved in:  {final_path_name_and_file}')







    def load(self, path, start_train=True):
        checkpoint_dict = torch.load(path)
        self.model.load_state_dict(checkpoint_dict["model"], strict=False)
        self.optimizer.load_state_dict(checkpoint_dict["optimizer"])
        self.start_epoch = checkpoint_dict["epoch"]

        if self.accelerator.is_main_process:
            self.ema_model.load_state_dict(checkpoint_dict["ema_model"])

        if start_train:
            self.train_loop()
        else:
            pass
