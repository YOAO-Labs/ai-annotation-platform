---
title: 用户手册
pageClass: docs-hub-page
aside: false
audience: [annotator, reviewer, project_admin, super_admin]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 用户手册

面向标注员、审核员、项目管理员、超级管理员和观察者。先按要完成的任务选择入口，再按角色补充权限和管理说明。

## 按任务进入

从下面六个常见任务开始：

<span id="开始" aria-hidden="true"></span>
<span id="数据与项目" aria-hidden="true"></span>
<span id="标注" aria-hidden="true"></span>
<span id="审核与导出" aria-hidden="true"></span>

<div class="doc-card-grid cols-3">
  <DocLinkCard title="领取并完成任务" desc="登录、打开已分派任务、完成首条标注并提交" href="/user-guide/getting-started" />
  <DocLinkCard title="导入数据" desc="上传图像数据集，确认导入结果并创建任务" href="/user-guide/datasets/import-images" />
  <DocLinkCard title="创建与分配项目" desc="创建项目、准备数据并把批次分配给标注员" href="/user-guide/workflows/new-project-end-to-end" />
  <DocLinkCard title="使用 AI 辅助" desc="选择 AI 能力、接管候选并处理异步任务" href="/user-guide/ai/" />
  <DocLinkCard title="审核标注" desc="检查质量、通过任务或退回修改" href="/user-guide/review/" />
  <DocLinkCard title="导出结果" desc="按目标格式导出已完成的标注数据" href="/user-guide/reference/export-formats" />
</div>

## 数据集与存储

数据管理员、项目管理员与超级管理员可统一管理托管上传、原地目录引用、数据集工作台筛选视图及平台多位置存储：

<div class="doc-card-grid cols-3">
  <DocLinkCard title="数据集与存储总览" desc="了解托管原件、原地引用、内容身份与存储治理分工" href="/user-guide/datasets/" />
  <DocLinkCard title="数据来源与增量同步" desc="接入 S3 / SFTP / 受控目录，执行扫描与增量同步" href="/user-guide/datasets/sources" />
  <DocLinkCard title="数据集工作台与视图" desc="浏览样本、筛选质量与状态、保存个人视图或冻结训练版本" href="/user-guide/datasets/workbench" />
  <DocLinkCard title="存储位置与连接器" desc="登记对象存储、受控目录与只读站点 Agent，配置九用途映射" href="/user-guide/datasets/storage-connections" />
</div>

## 在线学习

项目负责人可从训练数据准备一路完成模型回流，再根据数据增长启用自动训练：

<div class="doc-card-grid cols-3">
  <DocLinkCard title="在线学习总览" desc="了解数据版本、实验、候选模型与默认预标的关系" href="/user-guide/online-learning/" />
  <DocLinkCard title="完成第一轮与第二轮" desc="从已检查标注开始训练，再用人工修正的数据迭代" href="/user-guide/online-learning/quick-start" />
  <DocLinkCard title="自动训练策略" desc="按已审核数据的变化门槛与冷却时间触发下一轮" href="/user-guide/online-learning/automation" />
</div>

## 按角色定位

需要确认权限范围时，选择与你的工作身份对应的入口：

<div class="doc-card-grid cols-3">
  <DocLinkCard title="标注员" desc="接收任务、在工作台完成标注并提交" href="/user-guide/getting-started" />
  <DocLinkCard title="数据管理员 / 项目管理员" desc="管理数据集与来源、创建项目、分配任务并跟进进度" href="/user-guide/projects/" />
  <DocLinkCard title="审核员 / 质检员" desc="检查标注质量，通过或退回给标注员修正" href="/user-guide/review/" />
  <DocLinkCard title="超级管理员" desc="治理存储位置与站点 Agent、管理用户、注册 ML Backend 并查看系统状态" href="/user-guide/superadmin/" />
  <DocLinkCard title="观察者 Viewer" desc="查看平台公开内容，需要升级权限后参与标注" href="/user-guide/concepts" />
</div>
### 设置与支持

需要查概念、快捷键或账号设置时：

- [平台概念与术语](./concepts)：了解任务、批次和标注的关系。
- [工作台概览与快捷键](./workbench/)：查看界面分区和完整快捷键。
- [设置页](./reference/settings)：修改密码、通知偏好和标注偏好。
- 提交 BUG 或问题：使用工作台顶栏或其他页面右下角的反馈按钮；普通页面顶栏可查看当前前端版本。
- [常见问题 FAQ](./faq)：查找常见使用问题。
