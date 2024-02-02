"""
Creates a new meta data file which maps jsonl files to their repository/obfuscation
- based on binary-metadata.py which was created from the original binary folder (before preprocess.py was ran)
- many binaries were invalid after running preprocess.py on them
- script produces JSON file which contains info about the jsonl files which make up the actual dataset
created from preprocess.py

@author: Deniz BT (Jan 2024)
"""
import argparse
import json
import subprocess
import os
import glob
from tqdm import tqdm


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-type", type=str, default="dev")
    parser.add_argument("--dataset-folder", type=str,
                        default="dataset_larger/")
    parser.add_argument("--meta-data", type=str,
                        default="binaries-metadata.json")

    return parser.parse_args()


def main():
    args = get_args()
    new_file = f"{args.dataset_folder}/{args.set_type}-set-metadata.json"

    with open(args.meta_data, "r") as fp:
        content = json.load(fp)
    full_meta_data = {d["hash_name"]: d for d in content}
    print("Loaded full meta_data from JSON file.")

    temp_folder = f"{args.dataset_folder}/temp_{args.set_type}"
    subprocess.run(["mkdir", "-p", temp_folder])

    # create and start binary_data.json file
    with open(new_file, "w+") as f:
        f.write("[")
        f.close()

    # different methods depending on test or train set
    if args.set_type == "test" or args.set_type == "dev":
        subprocess.run(
            [
                "tar",
                "-C",
                temp_folder,
                "-xf",
                f"{args.dataset_folder}/{args.set_type}.tar",
            ]
        )

        for file in tqdm(os.listdir(temp_folder)):
            if file.endswith(".jsonl"):
                hash_name = file.split("_")[0]
                entry = full_meta_data[hash_name]

                # add the entry to the json fileå
                with open(new_file, "a+") as f:
                    # print(f"entry[0] {json.dumps(entry)}")
                    f.write(json.dumps(entry))
                    f.write(",\n")
                    f.close()

        # delete un-tarred folder
        subprocess.run(["rm", "-rf", temp_folder])
    else:  # train set
        for train_shard in glob.glob(f"{args.dataset_folder}/train-shard-*.tar"):
            subprocess.run(["mkdir", "-p", temp_folder])
            # untar train shard tar to the temp folder
            subprocess.run(["tar", "-C", temp_folder, "-xf", train_shard])
            print(f"Processing train shard {train_shard}")

            for file in os.listdir(temp_folder):
                if file.endswith(".jsonl"):
                    hash_name = file.split("_")[0]
                    entry = full_meta_data[hash_name]

                    # add the entry to the json file
                    with open(new_file, "a+") as f:
                        # print(f"entry {json.dumps(entry)}")
                        f.write(json.dumps(entry))
                        f.write(",\n")
                        f.close()
        # delete un-tarred folder from this shard
        subprocess.run(["rm", "-rf", temp_folder])

    # finish json file
    subprocess.run(f"sed -i '$ s/.$//' {new_file}", shell=True)
    with open(new_file, "a+") as f:
        f.write("]")
        f.close()

    print(
        f"Created meta-data json for all binary files in pre-processed dataset ({args.set_type})."
    )


if __name__ == "__main__":
    main()
