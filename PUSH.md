# 推送到 GitHub

仓库已经在本地初始化并提交完毕，远端也配置好了：

```
origin  git@github.com:Xpg74138/va-capture.git
```

只差在 GitHub 上先创建这个空仓库（SSH 密钥只能推送，不能建仓库）。

## 步骤

1. 打开 https://github.com/new
2. 仓库名填 **va-capture**
3. **不要**勾选 "Add a README file"（本地已有）
4. 点 Create repository
5. 回到终端执行：

```bash
cd "/home/xpg/Desktop/VA Capture"
git push -u origin main
```

之后每次改动：

```bash
git add -A
git commit -m "feat: ..."
git push
```

## 发布 .deb

```bash
./packaging/build-deb.sh          # 生成 build/va-capture_1.0.0_all.deb
gh release create v1.0.0 build/*.deb   # 或用网页 Release 上传
```
