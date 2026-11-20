import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autothink  # noqa: E402


def user(text='fix the bug'):
    return {'role': 'user', 'content': text}


def call(cid, name, **args):
    return {'role': 'assistant', 'content': '',
            'tool_calls': [{'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def result(cid, text):
    return {'role': 'tool', 'tool_call_id': cid, 'content': text}


SYS = {'role': 'system', 'content': 'You are a coding agent.'}


class Decide(unittest.TestCase):
    def test_new_message_plans_low(self):
        level, budget, anchor, why = autothink.decide([SYS, user()], 'auto')
        self.assertEqual((level, budget), ('low', None))
        self.assertIn('new message', why)

    def test_routine_action_does_not_think(self):
        msgs = [SYS, user(), call('1', 'edit', filePath='a.py'), result('1', 'Edit applied.')]
        self.assertEqual(autothink.decide(msgs, 'auto')[0], 'none')

    def test_failed_test_thinks_high(self):
        msgs = [SYS, user(), call('1', 'bash', command='pytest -q'), result('1', '=== 2 failed, 10 passed ===')]
        level, _b, anchor, why = autothink.decide(msgs, 'auto')
        self.assertEqual(level, 'high')
        self.assertIn('bash failed', why)
        self.assertIsNone(anchor)                       # plain auto keeps no pool
        self.assertEqual(autothink.decide(msgs, 'auto-flash', autothink.Pools())[2], 'f:1')

    def test_gradle_build_failure_thinks_high(self):
        msgs = [SYS, user(), call('1', 'bash', command='./gradlew :app:assembleDebug'),
                result('1', 'e: Foo.kt:3:1 Unresolved reference\nFAILURE: Build failed with an exception.')]
        self.assertEqual(autothink.decide(msgs, 'auto')[0], 'high')

    def test_lookup_that_finds_nothing_is_not_a_failure(self):
        msgs = [SYS, user(), call('1', 'bash', command='grep -rn missing_name src'), result('1', 'exit code 1')]
        level, _b, _a, why = autothink.decide(msgs, 'auto')
        self.assertEqual(level, 'low')          # still in the planning phase of the user message
        self.assertIn('still looking (1)', why)

    def test_level_survives_lookarounds_then_drops(self):
        msgs = [SYS, user()]
        for n in range(autothink.STICKY_MAX):
            msgs += [call(str(n), 'read', filePath='f%d' % n), result(str(n), 'contents')]
        self.assertEqual(autothink.decide(msgs, 'auto')[0], 'low')
        msgs += [call('x', 'read', filePath='more'), result('x', 'contents')]
        self.assertEqual(autothink.decide(msgs, 'auto')[0], 'none')

    def test_edit_with_diagnostics_is_a_failure(self):
        msgs = [SYS, user(), call('1', 'edit', filePath='a.ts'), result('1', 'LSP errors detected in a.ts')]
        self.assertEqual(autothink.decide(msgs, 'auto')[0], 'high')

    def test_only_auto_and_auto_flash(self):
        self.assertEqual(set(autothink.VARIANTS), {'auto', 'auto-flash'})
        for name in ('sometimes', 'auto-lite', 'off'):
            with self.assertRaises(ValueError):
                autothink.decide([user()], name)


class Pooled(unittest.TestCase):
    def test_pool_budget_shrinks_and_runs_out(self):
        pools = autothink.Pools()
        msgs = [SYS, user()]
        level, budget, anchor, _ = autothink.decide(msgs, 'auto-flash', pools)
        self.assertEqual((level, budget), ('low', 1500))
        pools.add(anchor, 1000)
        msgs += [call('1', 'read', filePath='a'), result('1', 'x')]
        level, budget, anchor2, _ = autothink.decide(msgs, 'auto-flash', pools)
        self.assertEqual((level, budget, anchor2), ('low', 500, anchor))     # same phase, same pool
        pools.add(anchor, 480)
        level, budget, _a, why = autothink.decide(msgs, 'auto-flash', pools)
        self.assertEqual((level, budget), ('none', None))
        self.assertIn('pool spent (1480/1500)', why)

    def test_each_failure_gets_a_fresh_pool(self):
        pools = autothink.Pools()
        msgs = [SYS, user(), call('1', 'bash', command='pytest'), result('1', '1 failed')]
        _l, budget, anchor, _ = autothink.decide(msgs, 'auto-flash', pools)
        pools.add(anchor, 1500)
        msgs += [call('2', 'edit', filePath='a'), result('2', 'ok'),
                 call('3', 'bash', command='pytest'), result('3', '1 failed')]
        level, budget, anchor3, _ = autothink.decide(msgs, 'auto-flash', pools)
        self.assertEqual((level, budget), ('high', 1500))
        self.assertNotEqual(anchor, anchor3)

    def test_same_message_in_new_request_shares_pool(self):
        pools = autothink.Pools()
        a = autothink.decide([SYS, user('task A')], 'auto-flash', pools)[2]
        b = autothink.decide([SYS, user('task A')], 'auto-flash', pools)[2]
        c = autothink.decide([SYS, user('task B')], 'auto-flash', pools)[2]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_pools_are_bounded(self):
        pools = autothink.Pools(max_phases=3)
        for k in 'abcd':
            pools.add(k, 10)
        self.assertEqual(pools.used('a'), 0)
        self.assertEqual(pools.used('d'), 10)


if __name__ == '__main__':
    unittest.main()
