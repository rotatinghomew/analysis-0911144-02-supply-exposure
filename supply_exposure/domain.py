from collections import defaultdict
from .common import finite, require, table, unique


def validate(data):
    nodes = table(data, 'nodes')
    edges = table(data, 'edges')
    orders = table(data, 'orders')
    unique(nodes)
    unique(orders)
    identifiers = {node['id'] for node in nodes}
    for node in nodes:
        finite(node['capacity'], 'capacity', 0)
    seen = set()
    for edge in edges:
        require(edge['parent'] in identifiers and edge['child'] in identifiers, '关系指向未知节点')
        require(bool(edge['group']), '部件组不能为空')
        key = edge['parent'], edge['child'], edge['group'], edge['valid_from']
        require(key not in seen, '关系重复')
        seen.add(key)
        require(finite(edge['units'], 'units') > 0, '需求系数必须为正')
        require(type(edge['valid_from']) is int, '认证日期必须为整数日')
    for order in orders:
        require(order['node'] in identifiers, '订单节点不存在')
        finite(order['quantity'], 'quantity', 0)
        require(type(order['due']) is int, '交付日期错误')


def reachable(data, start, day):
    children = defaultdict(list)
    for edge in data['edges']:
        if edge['valid_from'] <= day:
            children[edge['parent']].append(edge['child'])
    visited = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(children[current])
    return sorted(visited - {start})


def records(data):
    return data['orders']


def summarize(data):
    groups = {(edge['parent'], edge['group']) for edge in data['edges']}
    return {'nodes': len(data['nodes']), 'component_groups': len(groups), 'orders': [{'id': order['id'], 'quantity': order['quantity'], 'reachable_sources': reachable(data, order['node'], order['due'])} for order in data['orders']], 'meaning': '可达来源不等于可供量'}
