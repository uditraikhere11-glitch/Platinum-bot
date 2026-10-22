import os

def get_checkpoint_file(channel_id):
    return f".checkpoint_{channel_id}.txt"

def read_checkpoint(channel_id):
    file_path = get_checkpoint_file(channel_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return int(f.read().strip())
        except Exception as e:
            print(f"Error reading checkpoint: {e}")
    return None

def write_checkpoint(channel_id, message_id):
    file_path = get_checkpoint_file(channel_id)
    try:
        with open(file_path, 'w') as f:
            f.write(str(message_id))
    except Exception as e:
        print(f"Error writing checkpoint: {e}")

def get_last_date_file(channel_id):
    return f".last_date_{channel_id}.txt"

def read_last_date(channel_id):
    file_path = get_last_date_file(channel_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return f.read().strip()
        except Exception as e:
            print(f"Error reading last date: {e}")
    return None

def write_last_date(channel_id, date_str):
    file_path = get_last_date_file(channel_id)
    try:
        with open(file_path, 'w') as f:
            f.write(date_str)
    except Exception as e:
        print(f"Error writing last date: {e}")
