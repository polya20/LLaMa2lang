import os
import torch
from datasets import load_dataset
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    T5ForConditionalGeneration,
    T5Tokenizer,
    BitsAndBytesConfig
)
import json
import re
import gc
from tqdm import tqdm
import argparse
from translators.m2m import M2MTranslator
from translators.madlad import MADLADTranslator
from translators.opus import OPUSTranslator


# Find the max checkpoint number to continue from
def find_largest_checkpoint(checkpoint_location):
    pattern = r'upto_(\d+).json'
    files = os.listdir(checkpoint_location)
    numbers = [int(re.search(pattern, file).group(1)) for file in files if re.match(pattern, file)]
    if numbers:
        return max(numbers)
    else:
        return 0

# Group all records in a dataset by language so we can use a single model in a batched fashion
def group_records_by_language(dataset, lang_field):
    grouped_records = {}
    for record in dataset:
        lang = record[lang_field]
        if lang not in grouped_records:
            grouped_records[lang] = []
        grouped_records[lang].append(record)
    return grouped_records

def main():
    parser = argparse.ArgumentParser(description="Translate an instruct/RLHF dataset to a given target language using a variety of translation models")
    subparsers = parser.add_subparsers(dest='model', help='The model/architecture used for translation.')

    parser.add_argument('target_lang', type=str, 
                        help="The target language. Make sure you use language codes defined by the translation model you are using.")
    parser.add_argument('checkpoint_location', type=str, 
                        help="The folder the script will write (JSONized) checkpoint files to. Folder will be created if it doesn't exist.")

    parser.add_argument('--quant8', action='store_true',
                        help='Optional flag to load the translation model in 8 bits. Decreases memory usage, increases running time')
    parser.add_argument('--quant4', action='store_true',
                        help='Optional flag to load the translation model in 4 bits. Decreases memory usage, increases running time')
    parser.add_argument('--base_dataset', type=str, default="OpenAssistant/oasst1",
                        help="The base dataset to translate, defaults to OpenAssistant/oasst1")
    parser.add_argument('--base_dataset_text_field', type=str, default="text",
                        help="The base dataset's column name containing the actual text to translate. Defaults to text")
    parser.add_argument('--base_dataset_lang_field', type=str, default="lang",
                        help="The base dataset's column name containing the language the source text was written in. Defaults to lang")
    parser.add_argument('--checkpoint_n', type=int, default=400,
                        help="An integer representing how often a checkpoint file will be written out. To start off, 400 is a reasonable number.")
    parser.add_argument('--batch_size', type=int, default=10,
                        help="The batch size for a single translation model. Adjust based on your GPU capacity. Default is 10.")
    parser.add_argument('--max_length', type=int, default=None,
                        help='How much tokens to generate at most. More tokens might be more accurate for lengthy input but creates a risk of running out of memory. Default is unlimited.')
    parser.add_argument('--cpu', action='store_true',
                        help="Forces usage of CPU. By default GPU is taken if available.")

    parser_opus = subparsers.add_parser('opus', help='Translate the dataset using HelsinkiNLP OPUS models.')

    parser_madlad = subparsers.add_parser('madlad', help='Translate the dataset using Google\'s MADLAD models.')
    parser_madlad.add_argument('--model_size', type=str, default="3b", choices=['3b', '7b', '7b-bt'], help='The size of the MADLAD model to use. 7b-bt is the backtrained version (best to avoid unless you know what you are doing).')

    parser_m2m = subparsers.add_parser('m2m', help='Translate the dataset using Facebook\'s M2M models.')
    parser_m2m.add_argument('--model_size', type=str, default="418M", choices=['418M', '1.2B'], help='The size of the M2M model to use. Default is 418M')
    

    # Default arguments shared across models
    args = parser.parse_args()
    model = args.model
    target_lang = args.target_lang
    checkpoint_location = args.checkpoint_location
    quant4 = args.quant4
    quant8 = args.quant8
    base_dataset = args.base_dataset
    base_dataset_text_field = args.base_dataset_text_field
    base_dataset_lang_field = args.base_dataset_lang_field
    checkpoint_n = args.checkpoint_n
    batch_size = args.batch_size
    force_cpu = args.cpu
    
    device = torch.device("cuda:0" if torch.cuda.is_available() and not(force_cpu) else "cpu")

    if checkpoint_n % batch_size != 0:
        raise Exception("Checkpoint N must be a multiple of batch size!")

    # Load the base dataset that we want to translate
    dataset = load_dataset(base_dataset)

    # Set up quantization configs if required
    quant4_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )

    print(f"[---- LLaMa2Lang ----] Starting translation of {base_dataset} using {model} on device {device}")

    # Load the correct model
    if model == 'madlad':
        translator = MADLADTranslator(device, quant4, quant4_config, quant8, args.max_length, args.model_size)
    elif model == 'm2m':
        translator = M2MTranslator(device, quant4, quant4_config, quant8, args.max_length, args.model_size)
    else:
        translator = OPUSTranslator(device, quant4, quant4_config, quant8, args.max_length)

    # Loop through the actual data and translate
    with tqdm(total=sum(len(split) for split in dataset.values())) as pbar:
        for fold in dataset:
            records_by_lang = group_records_by_language(dataset[fold], base_dataset_lang_field)
            
            for source_lang, records in records_by_lang.items():
                lang_checkpoint_location = os.path.join(checkpoint_location, fold, f'from_{source_lang}')
                os.makedirs(lang_checkpoint_location, exist_ok=True)
                last_checkpoint_n = find_largest_checkpoint(lang_checkpoint_location)
                translated_texts = []
                print(f'[---- LLaMa2Lang ----] Got {len(records)} records for source language {source_lang}, skipping {last_checkpoint_n}')
                pbar.update(last_checkpoint_n)
                for cnt in range(last_checkpoint_n, len(records), batch_size):
                    # Translate a full batch
                    batch = records[cnt:cnt+batch_size]
                    texts_to_translate = [record[base_dataset_text_field] for record in batch]
                    # Offload translation to class implementation
                    translated_batch = translator.translate(texts_to_translate, source_lang, target_lang)
                    if translated_batch is not None:
                        # Combine original record with translated text
                        for record, translation in zip(batch, translated_batch):
                            record[base_dataset_text_field] = translation
                            record[base_dataset_lang_field] = target_lang
                            translated_texts.append(record)
                    
                    pbar.update(batch_size)

                    # Write out checkpoint file
                    if (cnt + batch_size) % checkpoint_n == 0 and cnt != 0:
                        print(f"[---- LLaMa2Lang ----] Writing out checkpoint #{str(cnt + batch_size)} for source language {source_lang}")
                        with open(os.path.join(lang_checkpoint_location, f'upto_{str(cnt + batch_size)}.json'), 'w', encoding='utf-8') as f:
                            json.dump(translated_texts, f)
                        translated_texts = []
                        # Free some memory
                        gc.collect()
                        if device == 'cuda':
                            torch.cuda.empty_cache()

                # Write checkpoint
                checkpoint_file = os.path.join(lang_checkpoint_location, f'upto_{cnt}.json')
                with open(checkpoint_file, 'w', encoding='utf-8') as f:
                    json.dump(batch, f)

            # One source language down, release the memory
            gc.collect()
            if device == 'cuda':
                torch.cuda.empty_cache()

if __name__ == "__main__":
    main()