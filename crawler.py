import os
import re
import sys
import time
import zipfile
import filecmp
from typing import List, Tuple
from shutil import copytree, rmtree
from urllib.request import urlretrieve

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement

URL = "http://huma.ysepan.com"
ZIP_FILE = "/tmp/huma.zip"
CHANGELOG_FILE = "changelog.txt"

try:
    os.unlink(ZIP_FILE)
except Exception:
    pass

# options = Options()
# options.headless = True
# os.environ['MOZ_HEADLESS'] = '1'
# browser = webdriver.Firefox(options=options) # type: ignore

timeout = 10

def eprint(s, *args):
    print(s, *args, file=sys.stderr)

def find_by_id(root, id: str) -> WebElement:
    i = 0
    while True:
        try:
            ele = root.find_element(by="id", value=id)
            return ele
        except Exception as e:
            if i > timeout:
                eprint(f"failed to find by id: {id}", repr(e))
                exit(1)
            time.sleep(1)
            i += 1

def find_by_tag(root, tag_name: str) -> WebElement:
    i = 0
    while True:
        try:
            ele = root.find_element(by="tag name", value=tag_name)
            return ele
        except Exception as e:
            if i > timeout:
                eprint(f"failed to find by tag_name: {tag_name}", repr(e))
                exit(1)
            time.sleep(1)
            i += 1


def find_elements_by_tag(root, tag_name: str) -> List[WebElement]:
    i = 0
    while True:
        items = root.find_elements(by="tag name", value=tag_name)
        if len(items) > 1:
            return items
        if i > timeout:
            eprint(f'failed to find elements by tag {tag_name}')
            exit(1)
        time.sleep(1)
        i += 1

def find_item_in_list(items, starts: str) -> WebElement:
    for item in items:
        if item.text.strip().startswith(starts):
            return item
    eprint(f'failed to find elements starting with {starts}')
    exit(1)

def find_item_in_list_by_tagname(items, tag_name: str, starts: str) -> Tuple[WebElement, WebElement]:
    for item in items:
        e = item.find_element(by='tag name', value=tag_name)
        if e.text.startswith(starts):
            return (item, e)
    eprint(f'failed to find item in list by tagname {tag_name} that starts with {starts}')
    exit(1)

def get_zip_and_extract(browser):
    browser.get(URL)

    menu_list = find_by_id(browser, 'dzx')
    items = find_elements_by_tag(menu_list, 'li')

    (target, a) = find_item_in_list_by_tagname(items, 'a', '03 虎码输入法下载')
    a.click()

    ul = find_by_tag(target, 'ul')

    items = find_elements_by_tag(ul, 'li')
    target = find_item_in_list(items, "④Mac")
    target.click()

    a = target.find_element(by="tag name", value="a")
    a.click()

    ul = find_by_tag(target, 'ul')
    items = find_elements_by_tag(ul, 'li')
    target = find_item_in_list(items, '鼠须管')
    target.click()

    a = target.find_element(by="tag name", value="a")
    a.click()

    ul = find_by_tag(target, 'ul')
    items = find_elements_by_tag(ul, 'li')
    target = find_item_in_list(items, "虎码秃版 鼠须管 （Mac）")
    a = find_by_tag(target, 'a')
    url = a.get_attribute('href')
    name = target.text.strip()

    assert url is not None
    assert name != ''

    assert ".zip" in name and "MB" in name
    m = re.match(r"虎码秃版 鼠须管 （Mac）(?P<date>.*)\.zip", name)
    if m is None:
        eprint(f"failed to extract date from filename: {name}")
        exit(1)
    date = m.group("date")

    r = ''
    if re.match(r"\d{4}\.\d{2}\.\d{2}", date):
        eprint(f"tag=v{date}")
        r = f'v{date}'
    else:
        # not necessarily date, might be a tag
        eprint(f"tag={date}")
        r = date

    eprint(f"downloading {name} with url {url}")

    urlretrieve(url, ZIP_FILE)
    ignore_files = [
        ".git",
        ".github",
        ".gitignore",
        "README.md",
        "crawler.py",
        "geckodriver.log",
    ]

    def delete_removed(diff, par='.'):
        for file in diff.left_only:
            delete_path = os.path.join(par, file)
            if os.path.isdir(delete_path):
                rmtree(delete_path)
            else:
                os.unlink(delete_path)
        for d, cmp in diff.subdirs.items():
            delete_removed(cmp, d)

    with zipfile.ZipFile(ZIP_FILE, "r", metadata_encoding="cp936") as zip_ref:
        folder = os.path.join("/tmp", zip_ref.filelist[0].filename[:-1])
        rmtree(folder, ignore_errors=True)
        zip_ref.extractall("/tmp")
        diff = filecmp.dircmp(".", folder, ignore=ignore_files)
        delete_removed(diff)
        copytree(folder, ".", dirs_exist_ok=True)

    os.unlink(ZIP_FILE)
    rmtree(folder)
    return r

def get_changelog(browser):
    browser.get(URL)

    menu_list = find_by_id(browser, 'dzx')
    items = find_elements_by_tag(menu_list, 'li')

    (target, a) = find_item_in_list_by_tagname(items, 'a', '05 虎码测评 更新日志')
    a.click()

    ul = find_by_tag(target, 'ul')

    items = find_elements_by_tag(ul, 'li')
    target = find_item_in_list(items, "虎码更新日志 ")
    a_list = find_elements_by_tag(target, 'a')
    name = target.text.splitlines()[0].strip()
    url = None
    for a in a_list:
        url_ = a.get_attribute('href')
        if url_ is not None and url_.startswith('http'):
            url = url_
            break
    assert url is not None

    m = re.match(r"虎码更新日志 (?P<date>.*).txt", name)
    if m is None:
        eprint(f"failed to extract date from filename: {name}")
        exit(1)
    date = m.group("date")
    r = ''
    if re.match(r"\d{4}\.\d{2}\.\d{2}", date):
        eprint(f"tag=v{date}")
        r = f'v{date}'
    else:
        # not necessarily date, might be a tag
        eprint(f"tag={date}")
        r = date
    eprint(f"downloading {name} with url {url}")

    urlretrieve(url, CHANGELOG_FILE)
    return r


def main():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    browser = webdriver.Chrome(options=chrome_options) # type: ignore
    tag = get_zip_and_extract(browser)
    eprint(f'got tag {tag}')
    browser.close()

    browser = webdriver.Chrome(options=chrome_options) # type: ignore
    changelog_tag = get_changelog(browser)
    eprint(f'got changelog tag {changelog_tag}')
    browser.close()
    if tag != changelog_tag:
        eprint("zip tag and changelog tag mismatch, add a pre-version")
        if tag > changelog_tag: # might has error if not in date format?
            print(f'tag={tag}-pre')
    else:
        print(f'tag={tag}')

if __name__ == "__main__":
    main()
