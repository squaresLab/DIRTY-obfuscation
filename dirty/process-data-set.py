"""
Given test/train set argument, untars test OR dev OR train shard sets and filters through .jsonl files
based on obfuscation type (adv obf, llvm fla, llvm sub, llvm bcf) given meta_data file
- DOES NOT alter the original tar file(s), it creates new tar files

@author: Deniz BT (Summer 2023 REUSE Student) [Jan 2024]
"""

import os
import subprocess
import argparse
import json
import fnmatch
import glob
from tqdm import tqdm


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-folder", type=str, default="dataset_larger"
    )  # folder with tar files
    parser.add_argument("--process-type", type=str, default="dev")
    return parser.parse_args()


def main():
    os.chdir(args.dataset_folder)
    args = get_args()

    meta_file = f"{args.process_type}-set-metadata.json"
    with open(meta_file, "r") as fp:
        content = json.load(fp)
    meta_data = {d["hash_name"]: d for d in content}
    print("Loaded meta_data from JSON file.")

    if args.process_type == "test" or args.process_type == "dev":
        process_test_or_dev_set(args, meta_data)
    elif args.process_type == "train":
        process_train_set(args, meta_data)


def move_files(args, meta_data, files):
    adv_folder = f"{args.process_type}_adv_obf"
    bcf_folder = f"{args.process_type}_llvm_bcf"
    fla_folder = f"{args.process_type}_llvm_fla"
    sub_folder = f"{args.process_type}_llvm_sub"
    none_folder = f"{args.process_type}_nones"
    for file in tqdm(files):
        if file.endswith(".jsonl"):
            file_name = file.split("_")[0]
            entry = meta_data[file_name]
            # print(entry)

            if entry["obfuscation"] == "none":
                subprocess.run(
                    ["mv", f"{bcf_folder}/{file}", f"{none_folder}/{file}"])
            elif entry["obfuscation"] == "adv-obfuscation":
                subprocess.run(
                    ["mv", f"{bcf_folder}/{file}", f"{adv_folder}/{file}"])
            elif entry["obfuscation"] == "llvm-obfuscation-fla":
                subprocess.run(
                    ["mv", f"{bcf_folder}/{file}", f"{fla_folder}/{file}"])
            elif entry["obfuscation"] == "llvm-obfuscation-sub":
                subprocess.run(
                    ["mv", f"{bcf_folder}/{file}", f"{sub_folder}/{file}"])


def process_train_set(args, meta_data):
    folders = [
        "train_adv_obf",
        "train_llvm_bcf",
        "train_llvm_fla",
        "train_llvm_sub",
        "train_nones",
    ]
    for f in folders:
        subprocess.run(["mkdir", "-p", f])

    for train_shard in glob.glob("train-shard-*.tar"):
        # untar train shard tar to the bcf folder
        subprocess.run(["tar", "-C", folders[1], "-xf", train_shard])

        # go through files in bcf_folder and move them to the correct folders
        bcf_files = os.listdir(folders[1])
        move_files(args, meta_data, bcf_files)

    # tar the folders
    subprocess.run(["tar", "-C", folders[0], "-cf", "train_adv_obf.tar", "."])
    subprocess.run(["tar", "-C", folders[1], "-cf", "train_llvm_bcf.tar", "."])
    subprocess.run(["tar", "-C", folders[2], "-cf", "train_llvm_fla.tar", "."])
    subprocess.run(["tar", "-C", folders[3], "-cf", "train_llvm_sub.tar", "."])
    subprocess.run(["tar", "-C", folders[4], "-cf", "train_nones.tar", "."])

    # count the number of files in each folder, add to file
    with open("train_obf_nums.txt", "w+") as f:
        f.write(
            f'# adv_obf files: {len(fnmatch.filter(os.listdir(folders[0]), "*.*"))}\n'
        )
        f.write(
            f'# llvm_bcf files: {len(fnmatch.filter(os.listdir(folders[1]), "*.*"))}\n'
        )
        f.write(
            f'# llvm_sub files {len(fnmatch.filter(os.listdir(folders[2]), "*.*"))}\n'
        )
        f.write(
            f'# llvm_fla files {len(fnmatch.filter(os.listdir(folders[3]), "*.*"))}\n'
        )
        f.write(
            f'# no obf files {len(fnmatch.filter(os.listdir(folders[4]), "*.*"))}\n'
        )

    # delete the original folders
    for folder in folders:
        subprocess.run(["rm", "-rf", folder])


def process_test_or_dev_set(args, meta_data):
    # create five different test sets (adv-obf, llvm-bcf, llvm-sub, llvm-fla, none)
    print(
        f"Creating {args.process_type} sets for each of the five types of obfuscations."
    )

    # create temp folders
    obf_folders = [
        f"{args.process_type}_adv_obf",
        f"{args.process_type}_llvm_bcf",
        f"{args.process_type}_llvm_fla",
        f"{args.process_type}_llvm_sub",
        f"{args.process_type}_nones",
    ]
    adv_folder = obf_folders[0]
    bcf_folder = obf_folders[1]
    fla_folder = obf_folders[2]
    sub_folder = obf_folders[3]
    none_folder = obf_folders[4]

    for folder in obf_folders:
        subprocess.run(["mkdir", "-p", folder])

    # untar test.tar to bcf_folder (will move binaries from there to the others as needed)
    subprocess.run(["tar", "-C", bcf_folder, "-xf",
                   f"{args.process_type}.tar"])

    bcf_files = os.listdir(bcf_folder)
    move_files(args, meta_data, bcf_files)

    # tar the folders
    subprocess.run(
        ["tar", "-C", bcf_folder, "-cf",
            f"{args.process_type}_llvm_bcf.tar", "."]
    )
    subprocess.run(
        ["tar", "-C", adv_folder, "-cf",
            f"{args.process_type}_adv_obf.tar", "."]
    )
    subprocess.run(
        ["tar", "-C", sub_folder, "-cf",
            f"{args.process_type}_llvm_sub.tar", "."]
    )
    subprocess.run(
        ["tar", "-C", fla_folder, "-cf",
            f"{args.process_type}_llvm_fla.tar", "."]
    )
    subprocess.run(
        ["tar", "-C", none_folder, "-cf",
            f"{args.process_type}_nones.tar", "."]
    )
    print(f"Tar-ed the new {args.process_type} partitions.")
    # subprocess.run(["tar", "cf", "test_tigress.tar"], cwd=tigress_folder)

    # count the number of files in each folder, add to file
    with open(f"{args.process_type}_obf_nums.txt", "w+") as f:
        f.write(
            f'# adv_obf files: {len(fnmatch.filter(os.listdir(adv_folder), "*.*"))}\n'
        )
        f.write(
            f'# llvm_bcf files: {len(fnmatch.filter(os.listdir(bcf_folder), "*.*"))}\n'
        )
        f.write(
            f'# llvm_sub files {len(fnmatch.filter(os.listdir(sub_folder), "*.*"))}\n'
        )
        f.write(
            f'# llvm_fla files {len(fnmatch.filter(os.listdir(fla_folder), "*.*"))}\n'
        )
        f.write(
            f'# no obf files {len(fnmatch.filter(os.listdir(none_folder), "*.*"))}\n'
        )

    # delete the original folders
    for folder in obf_folders:
        subprocess.run(["rm", "-rf", folder])


if __name__ == "__main__":
    main()
