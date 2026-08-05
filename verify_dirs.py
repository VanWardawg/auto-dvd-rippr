import os

# Create the directories
[os.makedirs(d, exist_ok=True) for d in [r'C:\Users\<user>\OneDrive\Documents\Coding\Auto-Ripper\app\autorippr', r'C:\Users\<user>\OneDrive\Documents\Coding\Auto-Ripper\app\tests']]
print('done')

# Verify they exist
dir1 = r'C:\Users\<user>\OneDrive\Documents\Coding\Auto-Ripper\app\autorippr'
dir2 = r'C:\Users\<user>\OneDrive\Documents\Coding\Auto-Ripper\app\tests'

print(f"\nVerification:")
print(f"Directory 1 exists: {os.path.isdir(dir1)} - {dir1}")
print(f"Directory 2 exists: {os.path.isdir(dir2)} - {dir2}")
