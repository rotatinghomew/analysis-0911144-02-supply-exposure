# 本地接口约定

所有接口使用 UTF-8 JSON，错误响应为带 error 字段的对象。GET /health 返回进程健康状态。POST /imports 接收 request_key 与 data，data 遵循业务资料契约；新导入返回 201，完全相同的请求重放返回 200。相同请求号对应不同资料返回 409；非法资料返回 400，不留下部分版本。

GET /revisions 列出 revision 和内容摘要。GET /records 返回业务原始记录；GET /summary 返回已有概览，两者都返回 revision、digest 和 result。可加 revision 查询参数固定历史资料，不提供时读取最新资料；资料不存在为 404。摘要对对象键顺序不敏感，对数组顺序敏感；资料内容相同可共享版本，导入请求回执相互独立。

未知路径为 404。现有客户端依赖这些路径和包装字段；后续增加业务分析接口时保留既有行为，不能把资料导入成功等同于业务分析已经形成结论。每个分析产物应声明所用数据版本，不能在客户端读取时偷偷替换成最新资料。

POST /exposure 在指定资料版本上评估中断情景，请求体为 {"scenarios": [...], "revision": N?}，revision 缺省读取最新资料；响应与既有查询同为 {revision, digest, result} 包装，分析不写入新版本。每个情景字段：name 可选（各情景需唯一）；stop 为停供节点数组；start/end 为停供时段整数日（0<=start<end）；horizon 为分析期整数日，缺省取订单最大交付日；unknown 可选，声明来源未核实的节点；fulfill_first 可选，声明同等总量下按序优先的订单。result.scenarios 为逐情景结果：每张订单的承诺量被三分为 confirmed（可确认）、unverified（无法确定）、exposed（确定缺口），三者之和等于承诺量；causes 给出无法确定部分按原因的上限（未生效认证、未知来源、循环转包，原因间可能重叠）；blocking 解释缺口沿物料依赖的来源；recovery 与 propagation 给出逐项单独恢复后的仍未确认量，单项恢复效果不可相加；certification_relief 评估每个未生效认证启用后可解除的量；assumptions 逐条声明本情景的核算口径。result.comparison 为跨情景对照：逐订单并列各情景结果、总量对照和 assumption_diff（哪些假设在情景间发生了变化）。情景非法为 400，资料版本不存在为 404，求解失败为 500。
