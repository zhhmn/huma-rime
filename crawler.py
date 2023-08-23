import os
import re
import sys
import time
import zipfile
from shutil import copytree, rmtree
from urllib.request import urlretrieve

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

URL = "http://huma.ysepan.com"
ZIP_FILE = 'huma.zip'

try:
    os.unlink(ZIP_FILE)
except Exception:
    pass

# options = Options()
# options.headless = True
# os.environ['MOZ_HEADLESS'] = '1'
# browser = webdriver.Firefox(options=options) # type: ignore
chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--no-sandbox")
browser = webdriver.Chrome(options=chrome_options)

timeout = 10

browser.get(URL)
i = 0
while True:
    try:
        menu_list = browser.find_element(by='id', value='menuList')
        break
    except Exception as e:
        if i > timeout:
            print("failed to find menuList", repr(e))
            exit(1)
        time.sleep(1)
        i += 1

while True:
    items = menu_list.find_elements(by='tag name', value='li')
    if len(items) > 1:
        break
    time.sleep(1)

target = None
for item in items:
    a = item.find_element(by='tag name', value='a')
    if a.text.startswith("05 虎码 鼠须管 iRime"):
        target = item
        a.click()
        break

if target is None:
    print("failed to find menu entry")
    exit(1)

i = 0
while True:
    try:
        ul = target.find_element(by='tag name', value='ul')
        break
    except Exception as e:
        if i > timeout:
            print("failed to get sublist after timeout")
            exit(1)
        time.sleep(1)
        i += 1

url = None
name = None
i = 0
while url is None and i < timeout:
    items = ul.find_elements(by='tag name', value='li')
    for item in items:
        if item.text.startswith('虎码秃版 鼠须管'):
            a = item.find_element(by='tag name', value='a')
            url = a.get_attribute('href')
            name = item.text
            break
    time.sleep(1)
    i += 1


if url is None:
    print("can't find zip file entry")
    exit(1)

browser.close()
assert name is not None
m = re.match(r'.*(?P<date>\d{4}\.\d{2}\.\d{2})\.zip', name)
if m is None:
    print(f"failed to extract filename: {name}")
    exit(1)
date = m.group('date')

print(f'tag=v{date}')
print(f'downloading {name} with url {url}', file=sys.stderr)

urlretrieve(url, ZIP_FILE)
with zipfile.ZipFile(ZIP_FILE, 'r', metadata_encoding='cp936') as zip_ref:
    folder = zip_ref.filelist[0].filename[:-1]
    zip_ref.extractall('.')
    copytree(folder, '.', dirs_exist_ok=True)

os.unlink(ZIP_FILE)
rmtree(folder)
