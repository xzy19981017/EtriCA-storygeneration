"""
@Desc:
@Reference:
@Notes:
WANDB is Weights and Biases Logger:
https://pytorch-lightning.readthedocs.io/en/stable/api/pytorch_lightning.loggers.wandb.html
"""

import sys
import json
import numpy as np
from pathlib import Path

from tqdm import tqdm

FILE_PATH = Path(__file__).absolute()
BASE_DIR = FILE_PATH.parent.parent.parent
sys.path.insert(0, str(BASE_DIR))  # run code in any path

from src.configuration.event_trigger.config_args import parse_args_for_config
from src.utils.file_utils import copy_file_or_dir, output_obj_to_file, pickle_save, pickle_load
from src.utils import nlg_eval_utils
from train import EventTriggerTrainer

from src.utils.limerick_metrics import LimerickEvaluator
from src.utils.limerick_rhyme_augmenter import RhymeAugmenter


class EventTriggerTester(EventTriggerTrainer):
    def __init__(self, args):
        # parameters
        super().__init__(args)
        self.generation_dir = self.experiment_output_dir / "gen_result"
        self.generation_dir.mkdir(parents=True, exist_ok=True)
        self.tokenizer = self.model.tokenizer
        self.model.eval()
        self.test_output = None
        self.src_file = None
        self.tgt_file = None
        self.gen_file = None
        self.eval_file = None
        self.ppl_file = None

        # customized
        self.dataset = self.model.test_dataloader().dataset
        self.src_file = self.dataset.src_file
        self.tgt_file = self.dataset.tgt_file
        self.output_prefix = self.dataset.event_file.stem if hasattr(self.dataset, "event_file")\
            else self.dataset.src_file.stem
        self.test_output_store_path = self.cache_dir.joinpath(f"{self.output_prefix}_test_output.pkl")
        self.gen_file = self.generation_dir / f"{self.output_prefix}_gen.txt"
        self.eval_file = self.generation_dir / f"{self.output_prefix}_eval.txt"

        self.limerick_sep_token = args.limerick_sep_token
        self.lim_eval = LimerickEvaluator(line_sep=self.limerick_sep_token)


    def test(self, ckpt_path=None):
        if ckpt_path is None:
            ckpt_path = self.checkpoints[-1]
        self.pl_trainer.test(model=self.model, ckpt_path=ckpt_path)

    def init_test_output(self):
        if self.test_output_store_path.exists():
            print(f"test output loaded from {self.test_output_store_path}")
            self.test_output = pickle_load(self.test_output_store_path)
        if self.test_output is None:
            self.model.store_test_output = True
            self.model.use_top_p = True
            self.model.top_p = 0.9
            self.test()
            self.test_output = self.model.test_output
            print(f"test output stored to {self.test_output_store_path}")
            pickle_save(self.test_output, self.test_output_store_path)
        if self.test_output is None:
            raise ValueError("self.test_output cannot be None")

    def generate(self):
        self.init_test_output()
        print(f"model {self.model.model_name} generating")
        print(f"src_file: {self.src_file}\ntgt_file: {self.tgt_file}\ngen_file: {self.gen_file}\n")
        print(f"test_loss: {self.test_output['test_loss']}")
        print(f"metrics: {self.test_output['log']}")

        copy_file_or_dir(self.src_file, self.generation_dir / "test.source.txt")
        copy_file_or_dir(self.tgt_file, self.generation_dir / "test.target.txt")

        with open(self.gen_file, "w", encoding="utf-8") as fw_out:
            fw_out.write("\n".join(self.test_output["preds"]))

    def eval_output(self):
        self.init_test_output()
        pred_lines = self.test_output["preds"]
        tgt_lines = self.test_output["tgts"]
        tgt_lines_toks, pred_lines_toks = \
            [self.tokenizer.tokenize(t) for t in tgt_lines], [self.tokenizer.tokenize(c) for c in pred_lines]

        metrics = {}
        # calculate perplexity
        lm_loss = self.test_output["log"]["test_lm_loss"].item() if "test_lm_loss" in self.test_output["log"] \
            else self.test_output['test_loss'].item()
        ppl = np.exp(lm_loss)
        ppl = round(ppl, 2)
        metrics["ppl"] = ppl
        # calculate bleu score
        nlg_eval_utils.calculate_bleu(ref_lines=tgt_lines_toks, gen_lines=pred_lines_toks, metrics=metrics)
        # calculate rouge score
        rouge_metrics = nlg_eval_utils.calculate_rouge(pred_lines=pred_lines, tgt_lines=tgt_lines)
        metrics.update(**rouge_metrics)
        # calculate repetition and distinction
        nlg_eval_utils.repetition_distinction_metric(pred_lines_toks, metrics=metrics, repetition_times=2)
        
        # Limerick metrics
        with open(self.src_file, "r") as f:
            src_lines = f.readlines()
        self.lim_eval.etrica(src_lines, pred_lines, metrics)
        
        key = sorted(metrics.keys())
        for k in key:
            print(k, metrics[k])
        print("=" * 10)

        print(f"model {self.model.model_name} eval {self.gen_file}")
        output_obj_to_file(json.dumps(metrics, indent=4), self.eval_file)
        return metrics

    def augment_rhymes(self):
        aug = RhymeAugmenter()

        with open(self.src_file, "r") as f:
            src_lines = f.readlines()
        pred_lines = self.test_output["preds"]

        num_change = 0

        with open(self.gen_file, 'w') as f:
            for i, (s, p) in enumerate(zip(src_lines, pred_lines)):
                l = p.split(self.limerick_sep_token)
                if len(l) != 5 or len(l[-1]) != 0:
                    pass
                else:
                    l_words = [x.split(" ") for x in l[:4]]
                    s_word = s.strip().split(" ")[-2]

                    base = [s_word, l_words[1][-1], s_word]
                    target = [0, 2, 3]

                    for b, t in zip(base, target):
                        change = aug.eval(b, l_words[t][-1])
                        if change != l_words[t][-1]:
                            num_change += 1
                            l_words[t][-1] = change

                    l = [" ".join(x) for x in l_words]
                f.write(". ".join(l) + ". \n")
                print(f"Limerick: {i}, total change: {num_change}")



if __name__ == '__main__':
    hparams = parse_args_for_config()
    tester = EventTriggerTester(hparams)

    # generate predicted stories
    tester.generate()
    if hparams.limerick_augment_rhyme:
        tester.augment_rhymes()

    tester.eval_output()