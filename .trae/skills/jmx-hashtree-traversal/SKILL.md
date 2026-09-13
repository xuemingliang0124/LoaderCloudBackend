---
name: "jmx-hashtree-traversal"
description: "JMeter JMX hashTree 配对结构、enabled 祖先链 DFS 与 ConstantThroughputTimer 读写约定。扫描或组装 JMX、查找/写入吞吐量定时器、判断节点是否生效时调用。"
---

# JMX hashTree 子树遍历与吞吐量定时器读写约定

适用于本仓库 master 的 JMX 扫描器与执行期组装器。任何"在 JMX 里找东西 / 改写东西"的需求都必须遵守本文约定，保证
**扫描读到的值 = 组装写入的目标**（两侧同口径）。

参考实现：`master/app/services/jmx_scanner.py`、`master/app/services/jmx_assembler.py`；
测试范式：`master/tests/test_jmx_scanner.py`、`master/tests/test_jmx_assembler.py`。

## 1. hashTree 配对结构（最容易写错的点）

JMX 不是嵌套 XML，而是**兄弟配对**：

```xml
<ThreadGroup .../>          <!-- 元素节点 -->
<hashTree>                  <!-- 紧邻的下一个兄弟 = 该元素的子树 -->
  <TransactionController .../>
  <hashTree> ... </hashTree> <!-- 控制器的子树，同样配对 -->
</hashTree>
```

- 遍历必须用"索引扫描 children 列表"：在位置 `i` 遇到元素节点，其子树是 `children[i+1]`（若它是 hashTree）。
- 不要用 `node.find("hashTree")`（hashTree 是兄弟不是子元素），也不要把 hashTree 自身当业务节点。
- 空容器写作 `<hashTree/>`。

骨架：

```python
children = list(tree)
for idx, child in enumerate(children):
    if child.tag == "hashTree":
        continue
    # child 是 TestElement；children[idx + 1] 是它的配对子树（存在时递归）
    subtree = children[idx + 1] if idx + 1 < len(children) and children[idx + 1].tag == "hashTree" else None
```

## 2. enabled 语义与祖先链

- `enabled` 属性缺省 = `"true"`；判断禁用：`node.get("enabled", "true") == "false"`。
- **禁用节点的整棵子树都不执行**（JMeter 语义）。DFS 时遇到禁用的控制器/取样器必须整棵剪枝，不得继续下钻。
- "生效节点" = 自身启用 **且** 从线程组到它的祖先链全部启用。
- 例外：扫描器对线程组本身**不剪枝**——禁用的线程组仍在结果中返回（`enabled=False`，参数为脚本静态配置，供前端展示开关）；只有 TestPlan 禁用才整体不返回。`compare_thread_groups` 只比对启用组（旧版启用组在新版被停用判差异 3009）。
- 扫描器收集"生效集合"时严格按上述剪枝；组装器对 `tps>0` 的复用判定只要求**祖先链启用**（定时器自身禁用也算命中——写入时顺带 set enabled=true，避免同组出现"旧禁用 + 新启用"两个定时器）。

## 3. 查找口径：线程组子树 DFS + 文档顺序第一个

- 作用域是**整个线程组子树**（组直接子级 + 事务控制器/逻辑控制器 + 取样器下，任意深度），不是组 hashTree 的直接子级。
- 按 XML 文档顺序 DFS，取第一个命中的节点；多个定时器共存时只认第一个，其余保留脚本作者配置。
- 参考实现：scanner 的 `_collect_effective_timers(tree)`；assembler 的 `_collect_timers(tree, include_self_disabled, out)`。改其中一侧的查找逻辑时，另一侧必须同步改。

## 4. ConstantThroughputTimer 结构与单位

```xml
<ConstantThroughputTimer ... enabled="true">
  <intProp name="calcMode">0</intProp>   <!-- 0 = 仅作用当前线程组 -->
  <doubleProp>
    <name>throughput</name>
    <value>6000.0</value>                <!-- 单位：样本/分钟 (TPM)，不是 TPS -->
    <savedValue>0.0</savedValue>
  </doubleProp>
</ConstantThroughputTimer>
```

- **TPS 与 TPM 换算：`tpm = tps * 60`；`tps = tpm / 60`**。场景 tps 按 Agent 均摊后是浮点份额，先均摊再 ×60。
- 写入格式 `f"{float(tpm):.1f}"`，读值用宽松浮点正则；支持 `${var}`（按用户变量作用域：线程组内 > 全局）与 `${__P(key, default)}`（解析 default）。
- 线程组启用状态：场景设置 `ScenarioScriptTG.enabled` 由组装器写入线程组节点 `enabled` 属性（`elem.set("enabled", "true"/"false")`，ElementTree 序列化后必显式存在）；禁用组其余参数仍照常写入（保留配置，重新启用即生效）。
- 组装策略（`_apply_throughput_timer`）：
  - `tps > 0`：复用第一个祖先链启用的定时器（含自身禁用者，写入即启用）；**全子树都没有可用定时器时**，才在组 hashTree 顶部 `insert(0, timer)` + `insert(1, hashTree)` 补建。
  - `tps == 0`（不限速）：递归把所有生效定时器 set enabled=false 并保留原值；不补建；禁用祖先下的不动。
- 作用域提醒：挂在控制器/取样器下的定时器，JMeter 限速作用域也局限于该分支，组装器尊重脚本原位置，不上提。

## 5. 测试约定

- 组装后用 `scan_jmx(assembled_bytes)` 回读做 round-trip 断言（扫描值必须等于写入目标）。
- 断言定时器位置时区分两个 helper：只读组直接子级的 `_tg_timers`（用于断言"未补建组级定时器"）与 `iter("ConstantThroughputTimer")` 全子树递归统计（用于断言总数/嵌套位置）。
- 必备用例矩阵：组直接子级、控制器嵌套、取样器下、祖先禁用剪枝、文档顺序优先、tps=0 递归禁用、定时器自身禁用但祖先启用（tps>0 启用复用）。
- 注意 ElementTree 输出后 `enabled` 一定显式存在；原始缺省属性读取时按 true 处理。

## 6. 环境与验证（Windows / PowerShell）

- 目录：`master/`；PowerShell **不支持 `&&`**，命令用 `;` 连接。
- 测试：`python -m pytest -q`（定向：`python -m pytest tests/test_jmx_scanner.py tests/test_jmx_assembler.py -q`；可用 `2>$null` 过滤 aiohttp unclosed session 噪音）。
- lint：`python -m ruff check app tests`；`python -m ruff format --check app tests`（行宽 100，需修正时 `python -m ruff format <file>`）。
