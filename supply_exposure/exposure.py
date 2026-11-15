"""面向业务的中断暴露分析。

在既有资料（节点产能、部件组、认证日期、订单）之上按情景评估承诺量，
把每张订单三分：可确认 / 无法确定 / 确定缺口。无法确定部分来自未生效认证、
情景声明的未知来源与循环转包，只给出上限，不对任何路径赋概率。

核算口径：
- 同一 parent 的不同 group 是并行部件，必须同时满足；同一 group 的不同
  child 是可互换来源，按各自 units 折算后合并使用。
- 节点产能是全情景共享的时段总量，所有订单、所有路径共用，不重复计数。
- 分配先求总量最大，再拉平最低满足率；优先级只能来自情景输入。
"""

import json
from collections import defaultdict, deque

from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from .common import require

TOL = 1e-6
MAX_SCENARIOS = 32

CAPACITY_RULE = '产能均匀分布于分析期，停供按重叠天数比例扣减有效产能'
CERT_RULE = 'valid_from≤交付日的认证计入可确认；未生效认证只影响无法确定部分'
ALLOCATION_RULE = '先总量最大，再拉平最低满足率；fulfill_first 中的订单在同等总量下按序优先'
UNCERTAINTY_RULE = '未生效认证、声明的未知来源与循环转包不计入可确认，也不赋概率，只计入无法确定部分'
SCENARIO_NOTES = [
    '可确认+无法确定+确定缺口=承诺量；无法确定部分未对任何路径赋概率',
    '单项恢复的效果不可相加；整单恢复需要所有部件组均可确认',
    '替代认证只能解除无法确定部分；确定缺口需要额外产能或新增来源',
    '无法确定部分按原因给出上限，原因之间可能重叠',
]


def _quantity(value):
    rounded = round(float(value), 6)
    return 0.0 if abs(rounded) < 1e-9 else rounded + 0.0


def _tolerance(value):
    return max(1e-9, abs(value) * 1e-9)


def _node_list(value, node_ids, field):
    require(isinstance(value, list) and all(isinstance(item, str) for item in value), f'{field} 必须是节点标识数组')
    require(len(set(value)) == len(value), f'{field} 不能重复')
    require(all(item in node_ids for item in value), f'{field} 指向未知节点')
    return list(value)


def _normalize(data, raw_scenarios):
    require(isinstance(raw_scenarios, list) and raw_scenarios, 'scenarios 必须是非空数组')
    require(len(raw_scenarios) <= MAX_SCENARIOS, f'情景数量不能超过 {MAX_SCENARIOS}')
    node_ids = {node['id'] for node in data['nodes']}
    order_ids = {order['id'] for order in data['orders']}
    dues = [order['due'] for order in data['orders']]
    normalized = []
    for index, raw in enumerate(raw_scenarios):
        require(isinstance(raw, dict), '情景必须是对象')
        name = raw.get('name', f'情景{index + 1}')
        require(isinstance(name, str) and name, '情景名称必须是非空字符串')
        start = raw.get('start')
        end = raw.get('end')
        require(type(start) is int and type(end) is int and 0 <= start < end, '停供时段必须为整数日且满足 0<=start<end')
        horizon = raw.get('horizon', max(dues) if dues else end)
        require(type(horizon) is int and horizon > 0, 'horizon 必须是正整数日')
        stop = _node_list(raw.get('stop', []), node_ids, 'stop')
        unknown = _node_list(raw.get('unknown', []), node_ids, 'unknown')
        priority = raw.get('fulfill_first', [])
        require(isinstance(priority, list) and all(isinstance(item, str) for item in priority), 'fulfill_first 必须是订单标识数组')
        require(len(set(priority)) == len(priority), 'fulfill_first 不能重复')
        require(all(item in order_ids for item in priority), 'fulfill_first 指向未知订单')
        normalized.append({'name': name, 'start': start, 'end': end, 'horizon': horizon,
                           'stop': stop, 'unknown': unknown, 'priority': priority})
    names = [scenario['name'] for scenario in normalized]
    require(len(set(names)) == len(names), '情景名称不能重复')
    return normalized


def _cyclic_components(data):
    """记录关系中的循环转包：大小大于 1 的强连通分量或自环。"""
    graph = {node['id']: [] for node in data['nodes']}
    self_loop = set()
    for edge in data['edges']:
        graph[edge['parent']].append(edge['child'])
        if edge['parent'] == edge['child']:
            self_loop.add(edge['parent'])
    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    components = []
    for root in graph:
        if root in index_of:
            continue
        index_of[root] = lowlink[root] = len(index_of)
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(graph[root]))]
        while work:
            node, neighbors = work[-1]
            descended = False
            for nxt in neighbors:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = len(index_of)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(graph[nxt])))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1 or component & self_loop:
                    components.append(sorted(component))
    components.sort()
    return components


def _entangled_groups(data, components):
    """循环分量内部的部件组：这些组的投入无法通过记录核实。"""
    membership = {}
    for component in components:
        for node in component:
            membership[node] = frozenset(component)
    entangled = set()
    for edge in data['edges']:
        component = membership.get(edge['parent'])
        if component and edge['child'] in component:
            entangled.add((edge['parent'], edge['group']))
    return entangled


def _closure(data, start, max_day):
    """从订单节点沿 parent→child 的物料依赖闭包；max_day 限制认证生效日。"""
    children = defaultdict(list)
    for edge in data['edges']:
        if max_day is None or edge['valid_from'] <= max_day:
            children[edge['parent']].append(edge['child'])
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in children[current]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _run(objective, rows, bounds, var_count):
    row_i, col_i, values, rhs = [], [], [], []
    for row, (terms, bound) in enumerate(rows):
        for col, val in terms.items():
            row_i.append(row)
            col_i.append(col)
            values.append(val)
        rhs.append(bound)
    matrix = coo_matrix((values, (row_i, col_i)), shape=(len(rows), var_count))
    result = linprog(objective, A_ub=matrix, b_ub=rhs, bounds=bounds, method='highs')
    if result.status != 0:
        raise RuntimeError(f'暴露分析求解失败: {result.message}')
    return result


def _solve(data, orders, capacity, usable, waived_groups, priorities):
    """求一组订单在给定产能、可用认证与豁免部件组下的交付量。

    返回每张订单的交付量、总量以及各节点产能的边际交付增益。
    分配规则：先总量最大，再按 fulfill_first 顺序优先，最后拉平最低满足率。
    """
    nodes = [node['id'] for node in data['nodes']]
    node_index = {node: i for i, node in enumerate(nodes)}
    order_count = len(orders)
    node_count = len(nodes)
    by_child = defaultdict(list)
    by_group = defaultdict(list)
    for edge_index, edge in enumerate(data['edges']):
        by_child[edge['child']].append(edge_index)
        by_group[(edge['parent'], edge['group'])].append(edge_index)

    def d_var(oi):
        return oi

    def g_var(oi, ni):
        return order_count + oi * node_count + ni

    f_var = {}
    for oi, order in enumerate(orders):
        for edge_index in sorted(usable[order['id']]):
            f_var[(oi, edge_index)] = order_count + order_count * node_count + len(f_var)
    var_count = order_count + order_count * node_count + len(f_var)

    rows = []
    # 物料平衡：节点产量须覆盖作为投入被消耗的量与直接交付的量
    for oi, order in enumerate(orders):
        usable_edges = usable[order['id']]
        for ni, node in enumerate(nodes):
            terms = {g_var(oi, ni): -1.0}
            if node == order['node']:
                terms[d_var(oi)] = 1.0
            for edge_index in by_child.get(node, ()):
                if edge_index in usable_edges:
                    key = f_var[(oi, edge_index)]
                    terms[key] = terms.get(key, 0.0) + 1.0
            rows.append((terms, 0.0))
    # 并行部件：每个已登记且未豁免的部件组都必须满足；可互换来源按 units 折算合并
    for (parent, group), edge_ids in by_group.items():
        if (parent, group) in waived_groups:
            continue
        for oi, order in enumerate(orders):
            terms = {g_var(oi, node_index[parent]): 1.0}
            for edge_index in edge_ids:
                if edge_index in usable[order['id']]:
                    key = f_var[(oi, edge_index)]
                    terms[key] = terms.get(key, 0.0) - 1.0 / data['edges'][edge_index]['units']
            rows.append((terms, 0.0))
    # 共享产能：所有订单、所有路径共用同一时段总量
    capacity_row = {}
    for ni, node in enumerate(nodes):
        capacity_row[node] = len(rows)
        rows.append(({g_var(oi, ni): 1.0 for oi in range(order_count)}, capacity[node]))

    bounds = [(0.0, orders[oi]['quantity']) for oi in range(order_count)]
    bounds += [(0.0, None)] * (var_count - order_count)

    objective = [0.0] * var_count
    for oi in range(order_count):
        objective[d_var(oi)] = -1.0
    res = _run(objective, rows, bounds, var_count)
    total = -res.fun
    gains = {node: max(0.0, -res.ineqlin.marginals[capacity_row[node]]) for node in nodes}

    extra = [({d_var(oi): -1.0 for oi in range(order_count)}, -(total - _tolerance(total)))]
    for target in priorities:
        hit = [oi for oi, order in enumerate(orders) if order['id'] == target]
        if not hit:
            continue
        oi = hit[0]
        objective = [0.0] * var_count
        objective[d_var(oi)] = -1.0
        achieved = -_run(objective, rows + extra, bounds, var_count).fun
        extra.append(({d_var(oi): -1.0}, -(achieved - _tolerance(achieved))))

    t_var = var_count
    objective = [0.0] * (var_count + 1)
    objective[t_var] = -1.0
    fill_rows = [({t_var: orders[oi]['quantity'], d_var(oi): -1.0}, 0.0)
                 for oi in range(order_count) if orders[oi]['quantity'] > 0]
    res_f = _run(objective, rows + extra + fill_rows, bounds + [(0.0, 1.0)], var_count + 1)
    delivered = {orders[oi]['id']: max(0.0, res_f.x[d_var(oi)]) for oi in range(order_count)}
    return {'delivered': delivered, 'total': total, 'gains': gains}


def _edge_brief(edge):
    return {'parent': edge['parent'], 'child': edge['child'], 'group': edge['group'],
            'units': edge['units'], 'valid_from': edge['valid_from']}


def _analyze_scenario(data, scenario, components, entangled):
    horizon = scenario['horizon']
    in_scope = [order for order in data['orders'] if 0 <= order['due'] <= horizon]
    out_scope = [order for order in data['orders'] if not 0 <= order['due'] <= horizon]
    recorded = {node['id']: node['capacity'] for node in data['nodes']}
    overlap = max(0, min(scenario['end'], horizon) - min(scenario['start'], horizon))
    factor = 1.0 - overlap / horizon
    stopped = set(scenario['stop'])
    unknown = set(scenario['unknown'])
    effective = {node: (capacity * factor if node in stopped else capacity) for node, capacity in recorded.items()}
    strict_capacity = {node: (0.0 if node in unknown else capacity) for node, capacity in effective.items()}

    strict_usable = {order['id']: {i for i, edge in enumerate(data['edges']) if edge['valid_from'] <= order['due']}
                     for order in in_scope}
    all_usable = {order['id']: set(range(len(data['edges']))) for order in in_scope}
    priority = [oid for oid in scenario['priority'] if any(order['id'] == oid for order in in_scope)]

    closures_eff = {order['id']: _closure(data, order['node'], order['due']) for order in in_scope}
    closures_all = {order['id']: _closure(data, order['node'], None) for order in in_scope}
    cyclic_nodes = set().union(*components) if components else set()

    strict = potential = certs = unknown_mode = circular_mode = None
    recovery = {node: None for node in scenario['stop']}
    relief = []
    if in_scope:
        strict = _solve(data, in_scope, strict_capacity, strict_usable, set(), priority)
        potential = _solve(data, in_scope, effective, all_usable, entangled, priority)
        certs = _solve(data, in_scope, strict_capacity, all_usable, set(), priority)
        unknown_mode = _solve(data, in_scope, effective, strict_usable, set(), priority)
        circular_mode = _solve(data, in_scope, strict_capacity, strict_usable, entangled, priority)
        for node in scenario['stop']:
            resumed = dict(strict_capacity)
            resumed[node] = 0.0 if node in unknown else recorded[node]
            recovery[node] = _solve(data, in_scope, resumed, strict_usable, set(), priority)
        candidates = {}
        for order in in_scope:
            for i, edge in enumerate(data['edges']):
                if edge['valid_from'] > order['due'] and edge['parent'] in closures_eff[order['id']]:
                    candidates[i] = edge
        for i, edge in sorted(candidates.items(), key=lambda kv: (kv[1]['parent'], kv[1]['child'], kv[1]['group'], kv[1]['valid_from'])):
            boosted = {oid: set(edges) for oid, edges in strict_usable.items()}
            for oid in boosted:
                boosted[oid].add(i)
            solved = _solve(data, in_scope, strict_capacity, boosted, set(), priority)
            relieves = {order['id']: _quantity(solved['delivered'][order['id']] - strict['delivered'][order['id']])
                        for order in in_scope}
            relieves = {oid: value for oid, value in relieves.items() if value > TOL}
            relief.append({'edge': _edge_brief(edge),
                           'within_horizon': edge['valid_from'] <= horizon,
                           'relieves': relieves,
                           'total_relief': _quantity(sum(relieves.values()))})
        relief.sort(key=lambda item: (-item['total_relief'], item['edge']['parent'], item['edge']['child'],
                                      item['edge']['group'], item['edge']['valid_from']))

    binding = {node: gain for node, gain in (strict['gains'] if strict else {}).items() if gain > TOL}
    orders_out = []
    totals = {'committed': 0.0, 'confirmed': 0.0, 'unverified': 0.0, 'exposed': 0.0}
    for order in in_scope:
        oid = order['id']
        committed = order['quantity']
        confirmed = strict['delivered'][oid]
        best = max(potential['delivered'][oid], confirmed)
        unverified = best - confirmed
        exposed = committed - best
        entry = {'id': oid, 'node': order['node'], 'due': order['due'], 'committed': committed,
                 'confirmed': _quantity(confirmed), 'unverified': _quantity(unverified), 'exposed': _quantity(exposed)}
        causes = {}
        for key, mode in (('pending_certification', certs), ('unknown_source', unknown_mode), ('circular', circular_mode)):
            delta = mode['delivered'][oid] - confirmed
            if delta > TOL:
                causes[key] = _quantity(delta)
        if causes:
            entry['causes'] = causes
        if committed - confirmed > TOL:
            blocks = []
            closure = closures_eff[oid]
            hit = sorted(closure & stopped)
            if hit:
                blocks.append({'type': 'stopped', 'nodes': hit, 'window': [scenario['start'], scenario['end']]})
            cap_hit = sorted(closure & set(binding))
            if cap_hit:
                blocks.append({'type': 'capacity', 'nodes': [
                    {'node': node, 'effective_capacity': _quantity(effective[node]), 'marginal_value': _quantity(binding[node])}
                    for node in cap_hit]})
            unk = sorted(closure & unknown)
            if unk:
                blocks.append({'type': 'unknown_source', 'nodes': unk})
            pending = [_edge_brief(edge) for edge in data['edges']
                       if edge['parent'] in closure and edge['valid_from'] > order['due']]
            if pending:
                blocks.append({'type': 'pending_certification', 'edges': pending})
            circ = sorted(closures_all[oid] & cyclic_nodes)
            if circ:
                blocks.append({'type': 'circular', 'nodes': circ})
            if blocks:
                entry['blocking'] = blocks
        if scenario['stop']:
            entry['recovery'] = [
                {'resume': node,
                 'confirmed': _quantity(recovery[node]['delivered'][oid]),
                 'unconfirmed': _quantity(committed - recovery[node]['delivered'][oid])}
                for node in scenario['stop']]
        orders_out.append(entry)
        totals['committed'] += committed
        totals['confirmed'] += confirmed
        totals['unverified'] += unverified
        totals['exposed'] += exposed

    propagation = [
        {'stopped': node,
         'affected_orders': [order['id'] for order in in_scope if node in closures_eff[order['id']]],
         'if_resumed': ({order['id']: _quantity(recovery[node]['delivered'][order['id']]) for order in in_scope}
                        if recovery[node] else {})}
        for node in scenario['stop']]

    result = {
        'name': scenario['name'],
        'assumptions': {
            'horizon': horizon,
            'window': {'start': scenario['start'], 'end': scenario['end']},
            'stopped': scenario['stop'],
            'unknown_sources': scenario['unknown'],
            'fulfill_first': scenario['priority'],
            'capacity_rule': CAPACITY_RULE,
            'certification_rule': CERT_RULE,
            'allocation_rule': ALLOCATION_RULE,
            'uncertainty_rule': UNCERTAINTY_RULE,
        },
        'effective_capacity': {node: _quantity(effective[node]) for node in scenario['stop']},
        'totals': {key: _quantity(value) for key, value in totals.items()},
        'orders': orders_out,
        'binding_capacities': [
            {'node': node, 'effective_capacity': _quantity(effective[node]), 'marginal_value': _quantity(gain)}
            for node, gain in sorted(binding.items(), key=lambda kv: (-kv[1], kv[0]))],
        'propagation': propagation,
        'certification_relief': relief,
        'circular_structures': components,
        'notes': SCENARIO_NOTES,
    }
    if out_scope:
        result['out_of_scope'] = [{'id': order['id'], 'due': order['due'], 'reason': '交付日在分析期外'}
                                  for order in out_scope]
    return result


def _compare(results, scenarios):
    order_ids = []
    committed = {}
    for result in results:
        for order in result['orders']:
            if order['id'] not in committed:
                order_ids.append(order['id'])
                committed[order['id']] = order['committed']
    rows = []
    for oid in order_ids:
        per_scenario = []
        for result in results:
            found = next((order for order in result['orders'] if order['id'] == oid), None)
            if found is None:
                per_scenario.append({'scenario': result['name'], 'out_of_scope': True})
            else:
                per_scenario.append({'scenario': result['name'], 'confirmed': found['confirmed'],
                                     'unverified': found['unverified'], 'exposed': found['exposed']})
        rows.append({'id': oid, 'committed': committed[oid], 'scenarios': per_scenario})
    fields = [('stopped', lambda s: s['stop']),
              ('window', lambda s: [s['start'], s['end']]),
              ('horizon', lambda s: s['horizon']),
              ('unknown_sources', lambda s: s['unknown']),
              ('fulfill_first', lambda s: s['priority'])]
    diff = []
    for field, getter in fields:
        values = [getter(scenario) for scenario in scenarios]
        if len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}) > 1:
            diff.append({'field': field, 'values': {scenario['name']: getter(scenario) for scenario in scenarios}})
    return {'orders': rows,
            'totals': [{'scenario': result['name'], **result['totals']} for result in results],
            'assumption_diff': diff}


def analyze(data, raw_scenarios):
    """在同一资料版本上评估一组中断情景，返回逐情景结果与跨情景对照。"""
    scenarios = _normalize(data, raw_scenarios)
    components = _cyclic_components(data)
    entangled = _entangled_groups(data, components)
    results = [_analyze_scenario(data, scenario, components, entangled) for scenario in scenarios]
    return {'scenarios': results, 'comparison': _compare(results, scenarios)}
