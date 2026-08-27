# python script open all .csv files in a folder and change ~^~ pattern to ;
import os

dirname = "./extract/20260813"
for filename in os.listdir(dirname):
    if filename.endswith(".csv"):
        filepath = os.path.join(dirname, filename)
        with open(filepath, "r") as f:
            content = f.read()
        content = content.replace("~^~", ";")
        with open(filepath, "w") as f:
            f.write(content)