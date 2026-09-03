import sys
import os

path = os.path.expanduser('~/.rote/flows/deppulse/main.ts')
with open(path, 'r') as f:
    text = f.read()

text = text.replace(' *   version: "0.0.1"\n  rote_version: "0.78.0"', ' *   version: "0.0.1"\n *   rote_version: "0.78.0"')

with open(path, 'w') as f:
    f.write(text)
print("done")
