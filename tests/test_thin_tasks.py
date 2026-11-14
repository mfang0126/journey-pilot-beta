"""Critical shared runtime contracts; synthetic screens, no phone access."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from journey_pilot import adapter
from journey_pilot.store import Store


class ThinTasks(unittest.TestCase):
    def test_unlabeled_ai_anchor_fallback_is_unique_and_grounded(self):
        from journey_pilot import douyin_ai
        header={'text':'评论 32','x':146,'y':1104,'w':196,'id':'header'}
        anchor={'text':'  ','id':'','class':'android.widget.TextView','x':368,'y':1103}
        for duplicates in (False, True):
            acted=[]
            def read(script, **kwargs):
                if script == adapter.OBSERVE_PIXELS:
                    return {'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':[{'text':'No tab recognized','confidence':1,'x':100,'y':200}]}
                if acted:
                    return {'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':[header,{'text':'输入问题或按住说话','id':'input'},
                            {'text':'Video summary'}, {'text':'A real summary paragraph that is longer than the loading threshold in this fixture.'}]}
                return {'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':[header,anchor]+([dict(anchor)] if duplicates else [])}
            with tempfile.TemporaryDirectory() as d, \
                 mock.patch.object(adapter,'status',return_value={'ready':True,'bound':'dev',
                                  'devices':[{'serial':'dev'}],'foreground':'com.ss.android.ugc.aweme/.MainActivity'}), \
                 mock.patch.object(adapter,'harness_read',side_effect=read), \
                 mock.patch.object(adapter,'run_action',side_effect=lambda a,**kw: acted.append(a) or {'ok':True}):
                result=douyin_ai.execute(Store(Path(d)),{'device_id':'dev'})
                self.assertEqual(result.status=='completed',not duplicates)
                self.assertEqual(len(acted),0 if duplicates else 1)
                if acted:
                    self.assertEqual((acted[0]['x'],acted[0]['y']),(368,1103))

    def test_xhs_card_hidden_under_bottom_navigation_is_not_tappable(self):
        from journey_pilot.read_post import own_card
        card = {'clickable': True, 'desc': '笔记,Title,来自Owner,0赞', 'y': 2437}
        nav = {'desc': '市集', 'y': 2493, 'h': 136}
        self.assertFalse(own_card(card, 'Title', 'Owner', [card, nav]))
        self.assertTrue(own_card({**card, 'y': 1800}, 'Title', 'Owner', [nav]))

    def test_unknown_task_never_touches_device(self):
        from journey_pilot.task_registry import execute
        with tempfile.TemporaryDirectory() as d, mock.patch.object(adapter, 'status') as status:
            result = execute(Store(Path(d)), {'task': 'arbitrary.py', 'device_id': 'dev'})
            self.assertNotEqual(result.status, 'completed')
            status.assert_not_called()

    def test_douyin_waits_for_content_and_checks_requested_title(self):
        from journey_pilot import douyin_ai
        header = {'text': '评论 32', 'x': 100, 'y': 500}
        controls = [header, {'text': '输入问题或按住说话'}]
        title = {'text': 'Example video summary'}
        body = {'text': 'A grounded summary paragraph about this video, not just a loading placeholder.'}
        calls = [0]
        def read(script, **kwargs):
            calls[0] += 1
            nodes = controls if calls[0] == 1 else controls + [title, body]
            return {'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': nodes}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev',
                               'foreground': 'com.ss.android.ugc.aweme/.MainActivity', 'devices': [{'serial':'dev'}]}), \
             mock.patch.object(adapter, 'harness_read', side_effect=read), \
             mock.patch.object(adapter, 'run_action') as action:
            result = douyin_ai.execute(Store(Path(d)), {'device_id': 'dev',
                                        'expected_summary': 'Example video', 'wait_seconds': 1})
            self.assertEqual(result.status, 'completed')
            self.assertIn(body['text'], result.data['summary_text'])
            action.assert_not_called()
            mismatch = douyin_ai.execute(Store(Path(d)), {'device_id': 'dev',
                                          'expected_summary': 'Other video', 'wait_seconds': 1})
            self.assertNotEqual(mismatch.status, 'completed')

    def test_shared_runtime_rejects_raw_coordinates_and_external_writes(self):
        from journey_pilot.task_runtime import execute, RouteBlocked
        def task(ctx):
            with self.assertRaises(RouteBlocked):
                ctx.act('input_text', text='not allowed')
            with self.assertRaises(RouteBlocked):
                ctx.act('tap_xy', x=100, y=200)
            return {'checked': True}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev', 'devices': [{'serial':'dev'}]}), \
             mock.patch.object(adapter, 'run_action') as action:
            result = execute(Store(Path(d)), {'device_id': 'dev'}, {'id': 'test'}, task)
            self.assertEqual(result.status, 'completed')
            action.assert_not_called()

    def test_missing_summary_body_asks_jev_and_reads_pixels(self):
        from journey_pilot import douyin_ai
        header = {'text': '评论 32', 'x': 100, 'y': 500}
        controls = [header, {'text': '输入问题或按住说话'}]
        def read(script, **kwargs):
            if script == adapter.OBSERVE_PIXELS:
                return {'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': [{'text': 'A visible summary paragraph read from the '
                                                       'screenshot because the tree had none.',
                                               'confidence': 0.9, 'x': 300, 'y': 700}]}
            return {'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': controls}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev',
                               'foreground': 'com.ss.android.ugc.aweme/.MainActivity', 'devices': [{'serial':'dev'}]}), \
             mock.patch.object(adapter, 'harness_read', side_effect=read), \
             mock.patch.object(adapter, 'run_action') as action, \
             mock.patch('journey_pilot.task_runtime._jev_choice', return_value=('pixel_read', 0.9)) as chooser:
            result = douyin_ai.execute(Store(Path(d)), {'device_id': 'dev', 'wait_seconds': 5})
        self.assertEqual(result.status, 'completed')
        chooser.assert_called_once()
        action.assert_not_called()  # reading pixels is not a screen action

    def test_low_confidence_jev_check_is_not_accepted(self):
        from journey_pilot.runner import check
        with mock.patch('journey_pilot.runner._jev_choice', return_value=('match', 0.01)) as jev:
            self.assertFalse(check([], {'jev': {'expect': 'match'}}))
            jev.assert_called_once()
        with mock.patch('journey_pilot.runner._jev_choice', return_value=('match', 0.9)):
            self.assertTrue(check([], {'jev': {'expect': 'match'}}))

    def test_pixel_text_is_refused_when_panel_is_gone(self):
        """OCR body on a changed screen must not be returned as this video's summary."""
        from journey_pilot import douyin_ai
        header = {'text': '评论 32', 'x': 100, 'y': 500}
        controls = [header, {'text': '输入问题或按住说话'}]
        switched = [{'text': '另一个页面'}]  # screen changed: panel gone
        calls = [0]
        def read(script, **kwargs):
            calls[0] += 1
            if script == adapter.OBSERVE_PIXELS:
                return {'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': [{'text': '一段足够长的别处文字，若被采信就会冒充本视频总结内容。',
                                               'confidence': 0.9, 'x': 300, 'y': 700}]}
            return {'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': controls if calls[0] <= 2 else switched}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev',
                               'foreground': 'com.ss.android.ugc.aweme/.MainActivity', 'devices': [{'serial':'dev'}]}), \
             mock.patch.object(adapter, 'harness_read', side_effect=read), \
             mock.patch.object(adapter, 'run_action'), \
             mock.patch('journey_pilot.task_runtime._jev_choice', return_value=('pixel_read', 0.9)):
            result = douyin_ai.execute(Store(Path(d)), {'device_id': 'dev', 'wait_seconds': 5})
        self.assertNotEqual(result.status, 'completed')
        self.assertIn('question_for_agent', (result.data.get('handoff') or {}))

    def test_decide_retries_once_on_low_confidence(self):
        from journey_pilot.task_runtime import execute
        answers = iter([('reobserve', 0.2), ('pixel_read', 0.9)])
        def task(ctx):
            return {'chosen': ctx.decide('x', {'reobserve': 'again', 'pixel_read': 'ocr'}, 'goal')}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev', 'devices': [{'serial':'dev'}]}), \
             mock.patch.object(adapter, 'harness_read', return_value={'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': [{'text': 'n'}]}), \
             mock.patch.object(adapter, 'run_action'), \
             mock.patch('journey_pilot.task_runtime._jev_choice',
                        side_effect=lambda *a, **k: next(answers)) as chooser:
            result = execute(Store(Path(d)), {'device_id': 'dev'}, {'id': 't'}, task)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.data['chosen'], 'pixel_read')
        self.assertEqual(chooser.call_count, 2)

    def test_blank_candidates_carry_coordinates_to_jev(self):
        from journey_pilot.task_runtime import execute
        seen = {}
        calls = []
        def task(ctx):
            ctx.observe()
            ctx.tap(lambda n: n.get('text') == '  ', 'blank tab',
                    fallback=lambda n: n.get('text') == '  ')
            return {'ok': True}
        def chooser(labels, spec):
            calls.append(1)
            seen['labels'] = list(labels)
            return ('abstain', 1.0)
        nodes = [{'text': '  ', 'class': 'android.widget.TextView', 'x': 100, 'y': 200},
                 {'text': '  ', 'class': 'android.widget.TextView', 'x': 500, 'y': 200}]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter, 'status', return_value={'ready': True, 'bound': 'dev',
                               'devices': [{'serial':'dev'}], 'foreground': 'app/.Main'}), \
             mock.patch.object(adapter, 'harness_read', return_value={'ok': True, 'geometry': {'width':1440,'height':3000,'orientation':0}, 'nodes': nodes}), \
             mock.patch.object(adapter, 'run_action'):
            result = execute(Store(Path(d)), {'device_id': 'dev'}, {'id': 't', 'freshness_seconds': 0},
                             task, chooser=chooser)
        self.assertNotEqual(result.status, 'completed')
        self.assertEqual(len(calls), 2)  # abstain -> one fresh-state retry
        self.assertTrue(all('x=' in lbl for lbl in seen['labels'][:2]),
                        f'blank labels lost coordinates: {seen["labels"]}')
