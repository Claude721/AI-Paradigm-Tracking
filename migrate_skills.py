import os
import shutil
from pathlib import Path
import re

PROMPTS_DIR = Path('prompts')
SKILLS_DIR = Path('skills')

if not SKILLS_DIR.exists():
    SKILLS_DIR.mkdir()

for md_file in PROMPTS_DIR.glob('*.md'):
    skill_name = md_file.stem
    content = md_file.read_text(encoding='utf-8')
    
    parts = content.split('\n---\n', 1)
    if len(parts) == 2:
        header, body = parts
        
        # Try to extract a simple description from the header
        lines = header.split('\n')
        title = lines[0].replace('# ', '').strip() if lines and lines[0].startswith('#') else skill_name
        
        desc_lines = [l.replace('> ', '').strip() for l in lines if l.startswith('> **用途**：')]
        desc = desc_lines[0].replace('**用途**：', '').strip() if desc_lines else f"Prompt for {skill_name}"
        
        new_content = f"""---
name: {skill_name}
description: {desc}
---

# {title}

{body.strip()}
"""
    else:
        new_content = f"""---
name: {skill_name}
description: Prompt for {skill_name}
---

{content.strip()}
"""
    
    skill_dir = SKILLS_DIR / skill_name
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / 'SKILL.md').write_text(new_content, encoding='utf-8')
    print(f"Migrated {skill_name}")

# Also move loader.py to skills/
if (PROMPTS_DIR / 'loader.py').exists():
    shutil.copy(PROMPTS_DIR / 'loader.py', SKILLS_DIR / 'loader.py')
    print("Copied loader.py")
    
