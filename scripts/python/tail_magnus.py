import time
import os
from pathlib import Path

def tail_magnus():
    root = Path(__file__).resolve().parents[2]
    log_file = str(root / "magnus_live.log")
    
    if not os.path.exists(log_file):
        print(f"⚠️ Waiting for {log_file} to be created...")
        while not os.path.exists(log_file):
            time.sleep(1)

    print("🚀 MAGNUS LIVE TAIL - Startad")
    print("-" * 50)

    with open(log_file, "r") as f:
        lines = f.readlines()
        for line in lines[-20:]:
            print_colored(line.strip())
            
        f.seek(0, os.SEEK_END)
        
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            print_colored(line.strip())

def print_colored(line):
    if "🚀" in line:
        print(f"\033[93m{line}\033[0m")
    elif "🎊" in line or "✅" in line:
        print(f"\033[92m{line}\033[0m")
    elif "❌" in line or "🔥" in line or "⚠️" in line:
        print(f"\033[91m{line}\033[0m")
    elif "🧠" in line:
        print(f"\033[94m{line}\033[0m")
    else:
        print(line)

if __name__ == "__main__":
    try:
        tail_magnus()
    except KeyboardInterrupt:
        print("\nClosing tail.")