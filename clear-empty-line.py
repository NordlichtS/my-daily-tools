import sys
import time
import os

def is_blank(line: str) -> bool:
    return line.strip() == ""

def main():
    # Expect a dragged-and-dropped file path
    if len(sys.argv) < 2:
        print("No file provided. Drag and drop a text file onto this script.")
        time.sleep(2)
        return

    file_path = sys.argv[1]

    if not os.path.isfile(file_path):
        print("Invalid file path.")
        time.sleep(2)
        return

    # Input max blank count
    try:
        max_blank = int(input("Enter max blank count (0-8): ").strip())
    except ValueError:
        print("Invalid input. Not an integer.")
        time.sleep(2)
        return

    if max_blank < 0 or max_blank > 8:
        print("Invalid range. Must be between 0 and 8.")
        time.sleep(2)
        return

    # Read file
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Failed to read file: {e}")
        time.sleep(2)
        return

    # Process lines
    result = []
    blank_count = 0

    for line in lines:
        if is_blank(line):
            blank_count += 1
            if blank_count <= max_blank:
                result.append(line if line.endswith("\n") else line + "\n")
        else:
            blank_count = 0
            result.append(line)

    # Overwrite file
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(result)
    except Exception as e:
        print(f"Failed to write file: {e}")
        time.sleep(2)
        return

    print("Processing complete. File overwritten.")
    time.sleep(2)

if __name__ == "__main__":
    main()
