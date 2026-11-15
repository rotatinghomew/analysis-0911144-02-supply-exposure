import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError

from supply_exposure.client import Client
from supply_exposure.domain import validate
from supply_exposure.exposure import analyze
from supply_exposure.repository import Repository
from supply_exposure.service import make_server

DEMO = json.loads(Path('fixtures/demo.json').read_text())


def scenario(**kwargs):
    base = {'stop': [], 'start': 0, 'end': 1, 'horizon': 5}
    base.update(kwargs)
    return base


def shared_capacity_data():
    return {
        'nodes': [{'id': 'p1', 'capacity': 100}, {'id': 'p2', 'capacity': 100}, {'id': 's', 'capacity': 10}],
        'edges': [
            {'parent': 'p1', 'child': 's', 'group': 'g', 'units': 1, 'valid_from': 0},
            {'parent': 'p2', 'child': 's', 'group': 'g', 'units': 1, 'valid_from': 0},
        ],
        'orders': [
            {'id': 'o1', 'node': 'p1', 'quantity': 8, 'due': 5},
            {'id': 'o2', 'node': 'p2', 'quantity': 8, 'due': 5},
        ],
    }


class ExposureTests(unittest.TestCase):
    def test_baseline_shared_capacity_and_binding(self):
        result = analyze(DEMO, [scenario()])
        s = result['scenarios'][0]
        self.assertEqual(s['totals'], {'committed': 75, 'confirmed': 60, 'unverified': 0, 'exposed': 15})
        orders = {o['id']: o for o in s['orders']}
        self.assertEqual(orders['o1']['confirmed'], 32)
        self.assertEqual(orders['o2']['confirmed'], 28)
        self.assertEqual(orders['o1']['exposed'], 8)
        self.assertEqual(orders['o2']['exposed'], 7)
        binding = {b['node']: b for b in s['binding_capacities']}
        self.assertIn('ore', binding)
        self.assertAlmostEqual(binding['ore']['marginal_value'], 1.0)
        self.assertNotIn('resin', binding)
        self.assertIn('ore', [n['node'] for b in orders['o1']['blocking'] if b['type'] == 'capacity' for n in b['nodes']])

    def test_interchangeable_sources_pool_but_parallel_parts_do_not(self):
        data = {
            'nodes': [{'id': 'p', 'capacity': 100}, {'id': 'a', 'capacity': 10}, {'id': 'b', 'capacity': 15}],
            'edges': [
                {'parent': 'p', 'child': 'a', 'group': 'g1', 'units': 1, 'valid_from': 0},
                {'parent': 'p', 'child': 'b', 'group': 'g1', 'units': 1, 'valid_from': 0},
            ],
            'orders': [{'id': 'o', 'node': 'p', 'quantity': 30, 'due': 5}],
        }
        order = analyze(data, [scenario()])['scenarios'][0]['orders'][0]
        self.assertEqual(order['confirmed'], 25)
        self.assertEqual(order['exposed'], 5)
        data['edges'][1]['group'] = 'g2'
        order = analyze(data, [scenario()])['scenarios'][0]['orders'][0]
        self.assertEqual(order['confirmed'], 10)
        self.assertEqual(order['exposed'], 20)

    def test_shared_capacity_not_double_counted(self):
        result = analyze(shared_capacity_data(), [scenario()])
        orders = {o['id']: o for o in result['scenarios'][0]['orders']}
        self.assertEqual(orders['o1']['confirmed'], 5)
        self.assertEqual(orders['o2']['confirmed'], 5)
        self.assertEqual(result['scenarios'][0]['totals']['confirmed'], 10)

    def test_stop_window_derates_capacity_and_recovery_is_partial(self):
        result = analyze(DEMO, [scenario(stop=['ore'], start=0, end=3)])
        s = result['scenarios'][0]
        self.assertEqual(s['effective_capacity'], {'ore': 24})
        orders = {o['id']: o for o in s['orders']}
        self.assertEqual(orders['o1']['confirmed'], 12.8)
        self.assertEqual(orders['o2']['confirmed'], 11.2)
        recovery = orders['o1']['recovery'][0]
        self.assertEqual(recovery, {'resume': 'ore', 'confirmed': 32, 'unconfirmed': 8})
        propagation = s['propagation'][0]
        self.assertEqual(propagation['stopped'], 'ore')
        self.assertEqual(propagation['affected_orders'], ['o1', 'o2'])
        self.assertEqual(propagation['if_resumed'], {'o1': 32, 'o2': 28})

    def test_single_supplier_recovery_does_not_clear_order(self):
        data = {
            'nodes': [{'id': n, 'capacity': 50} for n in ('unit', 'h', 'b', 'ore', 'chip')],
            'edges': [
                {'parent': 'unit', 'child': 'h', 'group': 'g1', 'units': 1, 'valid_from': 0},
                {'parent': 'unit', 'child': 'b', 'group': 'g2', 'units': 1, 'valid_from': 0},
                {'parent': 'h', 'child': 'ore', 'group': 'm', 'units': 1, 'valid_from': 0},
                {'parent': 'b', 'child': 'chip', 'group': 'c', 'units': 1, 'valid_from': 0},
            ],
            'orders': [{'id': 'o', 'node': 'unit', 'quantity': 40, 'due': 5}],
        }
        result = analyze(data, [scenario(stop=['ore', 'chip'], start=0, end=5)])
        order = result['scenarios'][0]['orders'][0]
        self.assertEqual(order['confirmed'], 0)
        recovery = {r['resume']: r for r in order['recovery']}
        self.assertEqual(recovery['ore']['confirmed'], 0)
        self.assertEqual(recovery['ore']['unconfirmed'], 40)
        self.assertEqual(recovery['chip']['confirmed'], 0)
        self.assertEqual(recovery['chip']['unconfirmed'], 40)
        self.assertTrue(any('不可相加' in note for note in result['scenarios'][0]['notes']))

    def test_cycle_is_unverified_not_confirmed(self):
        data = copy.deepcopy(DEMO)
        data['edges'].append({'parent': 'ore', 'child': 'unit', 'group': 'return', 'units': 1, 'valid_from': 0})
        s = analyze(data, [scenario()])['scenarios'][0]
        self.assertEqual(s['totals']['confirmed'], 0)
        self.assertEqual(s['totals']['unverified'], 75)
        self.assertEqual(s['totals']['exposed'], 0)
        orders = {o['id']: o for o in s['orders']}
        self.assertEqual(orders['o1']['causes'], {'circular': 40})
        self.assertEqual(orders['o2']['causes'], {'circular': 35})
        self.assertEqual(s['circular_structures'], [['body', 'ore', 'unit']])
        blocking_types = {b['type'] for b in orders['o1']['blocking']}
        self.assertIn('circular', blocking_types)

    def test_pending_certification_and_relief_ranking(self):
        data = copy.deepcopy(DEMO)
        data['nodes'].append({'id': 'ore2', 'capacity': 60})
        data['edges'].append({'parent': 'body', 'child': 'ore2', 'group': 'metal', 'units': 1, 'valid_from': 10})
        s = analyze(data, [scenario()])['scenarios'][0]
        orders = {o['id']: o for o in s['orders']}
        self.assertEqual(orders['o1']['confirmed'], 32)
        self.assertEqual(orders['o1']['unverified'], 8)
        self.assertEqual(orders['o1']['causes'], {'pending_certification': 8})
        self.assertEqual(orders['o2']['causes'], {'pending_certification': 7})
        relief = s['certification_relief']
        self.assertEqual(len(relief), 1)
        self.assertEqual(relief[0]['edge']['child'], 'ore2')
        self.assertFalse(relief[0]['within_horizon'])
        self.assertEqual(relief[0]['relieves'], {'o1': 8, 'o2': 7})
        self.assertEqual(relief[0]['total_relief'], 15)
        blocking_types = {b['type'] for b in orders['o1']['blocking']}
        self.assertIn('pending_certification', blocking_types)

    def test_declared_unknown_source(self):
        s = analyze(DEMO, [scenario(unknown=['ore'])])['scenarios'][0]
        orders = {o['id']: o for o in s['orders']}
        self.assertEqual(orders['o1']['confirmed'], 0)
        self.assertEqual(orders['o1']['unverified'], 32)
        self.assertEqual(orders['o1']['exposed'], 8)
        self.assertEqual(orders['o1']['causes'], {'unknown_source': 32})
        self.assertEqual(orders['o2']['confirmed'], 0)
        self.assertEqual(orders['o2']['unverified'], 28)
        self.assertEqual(orders['o2']['exposed'], 7)

    def test_scenario_comparison_and_assumption_diff(self):
        result = analyze(DEMO, [scenario(name='基准'), scenario(name='停供三日', stop=['ore'], start=0, end=3)])
        comparison = result['comparison']
        self.assertEqual([t['scenario'] for t in comparison['totals']], ['基准', '停供三日'])
        fields = {d['field'] for d in comparison['assumption_diff']}
        self.assertIn('stopped', fields)
        self.assertIn('window', fields)
        self.assertNotIn('horizon', fields)
        o1 = next(row for row in comparison['orders'] if row['id'] == 'o1')
        self.assertEqual(o1['scenarios'][0]['confirmed'], 32)
        self.assertEqual(o1['scenarios'][1]['confirmed'], 12.8)

    def test_order_beyond_horizon_is_out_of_scope(self):
        data = copy.deepcopy(DEMO)
        data['orders'][0]['due'] = 20
        s = analyze(data, [scenario()])['scenarios'][0]
        self.assertEqual([o['id'] for o in s['orders']], ['o2'])
        self.assertEqual(s['out_of_scope'], [{'id': 'o1', 'due': 20, 'reason': '交付日在分析期外'}])
        self.assertEqual(s['totals']['committed'], 35)
        self.assertEqual(s['totals']['confirmed'], 35)

    def test_fulfill_first_priority(self):
        result = analyze(shared_capacity_data(), [scenario(fulfill_first=['o1'])])
        orders = {o['id']: o for o in result['scenarios'][0]['orders']}
        self.assertEqual(orders['o1']['confirmed'], 8)
        self.assertEqual(orders['o2']['confirmed'], 2)
        self.assertEqual(result['scenarios'][0]['totals']['confirmed'], 10)

    def test_deterministic(self):
        first = analyze(DEMO, [scenario(stop=['ore'], start=0, end=3)])
        second = analyze(DEMO, [scenario(stop=['ore'], start=0, end=3)])
        self.assertEqual(first, second)

    def test_invalid_scenarios(self):
        with self.assertRaises(ValueError):
            analyze(DEMO, [])
        with self.assertRaises(ValueError):
            analyze(DEMO, [{'stop': ['ore'], 'start': 3, 'end': 1}])
        with self.assertRaises(ValueError):
            analyze(DEMO, [scenario(stop=['ghost'])])
        with self.assertRaises(ValueError):
            analyze(DEMO, [scenario(unknown=['ghost'])])
        with self.assertRaises(ValueError):
            analyze(DEMO, [scenario(fulfill_first=['ghost'])])
        with self.assertRaises(ValueError):
            analyze(DEMO, [scenario(horizon=0)])
        with self.assertRaises(ValueError):
            analyze(DEMO, [scenario(name='x'), scenario(name='x')])

    def test_exposure_fixture_invariants(self):
        payload = json.loads(Path('fixtures/exposure.json').read_text())
        validate(payload['data'])
        result = analyze(payload['data'], payload['scenarios'])
        self.assertEqual(len(result['scenarios']), 3)
        for s in result['scenarios']:
            for order in s['orders']:
                self.assertAlmostEqual(order['confirmed'] + order['unverified'] + order['exposed'],
                                       order['committed'], places=6)
            self.assertAlmostEqual(s['totals']['confirmed'] + s['totals']['unverified'] + s['totals']['exposed'],
                                   s['totals']['committed'], places=6)
        self.assertEqual(result['scenarios'][0]['circular_structures'], [['jig', 'machining']])
        chip_stop = result['scenarios'][1]
        o2 = next(o for o in chip_stop['orders'] if o['id'] == 'o2')
        self.assertEqual(o2['confirmed'], 0)
        self.assertGreater(o2['unverified'], 0)
        self.assertGreater(o2['causes']['circular'], 0)
        relief_children = {r['edge']['child'] for s in result['scenarios'] for r in s['certification_relief']}
        self.assertIn('resin_new', relief_children)


class ExposureHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / 'data.sqlite3')
        self.server = make_server(self.repository)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def test_http_roundtrip_revision_pinning_and_errors(self):
        first = self.client.import_data(DEMO, 'v1')
        changed = copy.deepcopy(DEMO)
        changed['nodes'][0]['capacity'] = 600
        second = self.client.import_data(changed, 'v2')
        self.assertNotEqual(first['revision'], second['revision'])

        response = self.client.exposure([scenario(stop=['ore'], start=0, end=3)], first['revision'])
        self.assertEqual(response['revision'], first['revision'])
        self.assertEqual(response['digest'], first['digest'])
        s = response['result']['scenarios'][0]
        self.assertEqual(s['totals']['confirmed'], 24)
        self.assertEqual(s['assumptions']['stopped'], ['ore'])

        latest = self.client.exposure([scenario()])
        self.assertEqual(latest['revision'], second['revision'])
        self.assertEqual(latest['result']['scenarios'][0]['totals']['confirmed'], 75)

        self.assertEqual(len(self.client.request('/revisions')['revisions']), 2)

        with self.assertRaises(HTTPError) as caught:
            self.client.exposure([scenario()], 999)
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.client.exposure([], first['revision'])
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.client.exposure([{'stop': ['ore'], 'start': 3, 'end': 1}], first['revision'])
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('/exposure', {})
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
