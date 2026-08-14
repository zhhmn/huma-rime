来自虎码[网盘](http://huma.ysepan.com/)

**03 虎码输入法下载** / **④Mac** / **鼠须管** / **虎码秃版 鼠须管 （Mac）...zip**

放一份在git上用于版本管理和更方便的安装。

安装方法：

1. 复制所有文件到rime[用户配置目录](https://github.com/rime/home/wiki/UserData)(注意备份之前的文件)。
2. deploy

也可使用 [Plum](https://github.com/rime/plum) 安装或更新：

```bash
bash rime-install zhhmn/huma-rime:huma
```

Plum 安装过程不包含 `01 双拼反查配置（自然码 小鹤 微软）` 目录，因为 Plum 无法正确处理该带空格的路径。需要自然码、小鹤或微软双拼反查的用户，请从仓库对应子目录手动复制 `PY_c.custom.yaml` 到 Rime 用户配置目录，再重新部署。

以下文件不参与 Rime 运行，复制到 Rime 用户配置目录后可删：

- `README.md`
- `crawler.py`
- `crawler_http.py`
- `huma.recipe.yaml`
- `.github` 目录
