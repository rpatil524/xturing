from pathlib import Path
from typing import Any, List, Optional, Union

import evaluate
import torch
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_int8_training
from transformers import AutoModelForCausalLM, AutoTokenizer

from xturing.config import DEFAULT_DTYPE
from xturing.engines.base import BaseEngine
from xturing.utils.loss_fns import CrossEntropyLoss


class CausalEngine(BaseEngine):
    def __init__(
        self,
        *,
        model_name: Optional[str] = None,
        weights_path: Optional[Union[str, Path]] = None,
        model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
        load_8bit: Optional[bool] = False,
    ):
        self.model_name = model_name

        if weights_path is not None:
            assert Path(
                weights_path
            ).is_dir(), "The weights path should be a existing directory"
            if load_8bit:
                self.model = AutoModelForCausalLM.from_pretrained(
                    weights_path,
                    torch_dtype=DEFAULT_DTYPE,
                    load_in_8bit=True,
                    device_map="auto",
                )
                self.model = prepare_model_for_int8_training(self.model)
            else:
                self.model = AutoModelForCausalLM.from_pretrained(
                    weights_path, torch_dtype=DEFAULT_DTYPE
                )
            self.tokenizer = AutoTokenizer.from_pretrained(weights_path)
        elif model is not None and tokenizer is not None:
            self.model = model
            self.tokenizer = tokenizer
        elif model_name is not None:
            if load_8bit:
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    torch_dtype=DEFAULT_DTYPE,
                    load_in_8bit=True,
                    device_map="auto",
                )
                self.model = prepare_model_for_int8_training(self.model)
            else:
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_name, torch_dtype=DEFAULT_DTYPE
                )
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        else:
            raise ValueError(
                "Please provide a model_name, the weights path or model and tokenizer."
            )

        self.loss_fct = CrossEntropyLoss()
        self.load_8bit = load_8bit

    def training_step(self, batch):
        if self.load_8bit:
            with torch.autocast("cuda", dtype=torch.float16):
                outputs = self.model(
                    input_ids=batch["input_ids"],
                    attention_mask=batch.get("attention_mask", None),
                )
        else:
            outputs = self.model(
                input_ids=batch["input_ids"],
                attention_mask=batch.get("attention_mask", None),
            )

        if "label_mask" in batch:
            loss = self.loss_fct(
                outputs.get("logits"), batch["targets"], mask=batch["label_mask"]
            )
        else:
            loss = self.loss_fct(outputs.get("logits"), batch["targets"])

        return loss

    def validation_step(self, batch):
        metrics = evaluate.load("accuracy")
        outputs = self.model(
            input_ids=batch["input_ids"],
            attention_mask=batch.get("attention_mask", None),
        )

        logits = outputs.get("logits")
        preds = torch.argmax(logits, -1)
        acc = metrics.compute(preds, batch["labels"])

        return acc

    def save(self, saving_path: Union[str, Path]):
        self.model.save_pretrained(saving_path)
        self.tokenizer.save_pretrained(saving_path)


class CausalLoraEngine(CausalEngine):
    def __init__(
        self,
        *,
        model_name: Optional[str] = None,
        weights_path: Optional[Union[str, Path]] = None,
        model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
        load_8bit: Optional[bool] = False,
        target_modules: Optional[Union[List[str], str]] = None,
    ):
        super().__init__(
            model_name=model_name,
            weights_path=weights_path,
            model=model,
            tokenizer=tokenizer,
            load_8bit=load_8bit,
        )
        peft_config = LoraConfig(
            r=8,
            lora_alpha=32,
            target_modules=target_modules,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
        )

        self.model = get_peft_model(self.model, peft_config)
        self.model.print_trainable_parameters()

        self.loss_fct = CrossEntropyLoss()
