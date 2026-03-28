import os
import random
import uuid

def create_mock_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)

def generate_test_data(base_dir="test_data"):
    mac_dir = os.path.join(base_dir, "source_mac")
    seagate_dir = os.path.join(base_dir, "source_seagate")
    iphone_dir = os.path.join(base_dir, "source_iphone")
    
    if os.path.exists(base_dir):
        print(f"Cleaning existing {base_dir}...")
        import shutil
        shutil.rmtree(base_dir)

    print(f"[*] Generating 1500+ mock images and 75+ videos...")
    
    # 1. Create unique "Master" contents
    unique_contents = [str(uuid.uuid4()) for _ in range(1000)]
    
    # 2. Distribute files across THREE sources
    for i, content in enumerate(unique_contents):
        ext = ".jpg" if i < 925 else ".mp4"
        
        # Scenario A: File only on Mac (Unique)
        if i % 4 == 0:
            create_mock_file(os.path.join(mac_dir, f"mac_only_{i}{ext}"), content)
        
        # Scenario B: File only on Seagate (Unique)
        elif i % 4 == 1:
            create_mock_file(os.path.join(seagate_dir, f"seagate_only_{i}{ext}"), content)
            
        # Scenario C: File only on iPhone (Unique)
        elif i % 4 == 2:
            create_mock_file(os.path.join(iphone_dir, f"iphone_only_{i}{ext}"), content)

        # Scenario D: Triple Duplicate (Mac, Seagate, AND iPhone)
        else:
            create_mock_file(os.path.join(mac_dir, f"dupe_on_mac_{i}{ext}"), content)
            create_mock_file(os.path.join(seagate_dir, f"dupe_on_seagate_{i}{ext}"), content)
            create_mock_file(os.path.join(iphone_dir, f"original_on_iphone_{i}{ext}"), content)

    # 3. Create some "Multi-Duplicates" (3+ copies)
    for i in range(20):
        content = f"triple_dupe_{i}"
        create_mock_file(os.path.join(mac_dir, f"triple_1_{i}.jpg"), content)
        create_mock_file(os.path.join(mac_dir, f"triple_2_{i}.jpg"), content)
        create_mock_file(os.path.join(seagate_dir, f"triple_master_{i}.jpg"), content)

    print(f"[+] Done! Mock data created at {os.path.abspath(base_dir)}")
    print(f"    - Mac folder: {mac_dir}")
    print(f"    - Seagate folder: {seagate_dir}")

if __name__ == "__main__":
    generate_test_data()
