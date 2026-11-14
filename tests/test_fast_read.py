"""Critical guards for the read-only fast path; fixtures never touch a phone."""
import contextlib
import io
import unittest
from journey_pilot import adapter, session
from journey_pilot.store import Store, read_json
from pathlib import Path
import tempfile
from unittest import mock

class Evidence(unittest.TestCase):
    def test_navigation_record_and_opt_in_observation(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d))
            store.create_run('r_test', {'status':'running', 'device_id':'dev'})
            session._write_lease(store,'r_test','token')
            request={'run_id':'r_test','lease_token':'token','device_id':'dev'}
            action={'type':'tap_xy','x':100,'y':200,'step_id':'tap',
                    'observation_id':'o_prior','locator':{'text':'我'}}
            with mock.patch.object(adapter,'harness_read',return_value={'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':[{'text':'我'}]}):
                observed=session.observe(store,request)
            action['observation_id']=observed.data['observation_id']
            with mock.patch.object(adapter,'run_action',return_value={'ok':True}):
                session.act(store,{**request,'request_id':'a','action':action})
            events=[e for e in store.read_events('r_test') if e['event'].startswith('action_')]
            self.assertEqual(events[0].get('execution',{}).get('x'),100)
            self.assertEqual(events[0].get('execution',{}).get('observation_id'),action['observation_id'])
            self.assertGreaterEqual(events[1].get('duration_ms',-1),0)
            with mock.patch.object(adapter,'harness_read',return_value={'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':[{'text':'sample'}]}):
                out=session.observe(store,{**request,'persist_tree':True})
            path=out.data.get('evidence_path')
            self.assertIsNotNone(path)
            self.assertEqual(read_json(Path(path))['nodes'],[{'text':'sample'}])

class ReadOnlyRoute(unittest.TestCase):
    def test_busy_device_and_low_confidence_never_act(self):
        from journey_pilot import read_post
        from journey_pilot.runner import _device_lock
        request={'device_id':'dev','title':'Test note','author':'Owner','jev':True}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(adapter,'status') as status:
            store=Store(Path(d))
            with _device_lock(store,'dev'):
                out=read_post.execute(store,request)
            self.assertEqual((out.error or {}).get('code'),'device_busy')
            status.assert_not_called()
        nodes=[{'desc':'我','clickable':True,'x':x,'y':200} for x in (100,200)]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter,'status',return_value={'ready':True,'bound':'dev',
                               'foreground':'com.xingin.xhs/.MainActivity','devices':[{'serial':'dev'}]}), \
             mock.patch.object(adapter,'harness_read',return_value={'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':nodes}), \
             mock.patch.object(adapter,'run_action') as action, \
             mock.patch.object(read_post,'_jev_choice',return_value=('n0',.56)) as chooser:
            out=read_post.execute(Store(Path(d)),request)
            self.assertNotEqual(out.status,'completed')
            # below the 0.65 fuse: one fresh-state retry, then handoff, never a click
            self.assertEqual(chooser.call_count,2)
            action.assert_not_called()
            self.assertIn('question_for_agent',(out.data.get('handoff') or {}))

    def test_reusable_route_reads_post_without_external_writes(self):
        from journey_pilot import read_post
        home=[{'text':'','desc':'我','clickable':True,'x':100,'y':200}]
        profile=[{'text':'笔记','desc':'已选定笔记'},
                 {'text':'','desc':'笔记,Test note,来自Owner,0赞','clickable':True,'x':150,'y':250}]
        post=[{'text':'Owner'},{'text':'Test note'},{'text':'公开可见'},
              {'text':'Body text'},{'text':'评论','desc':'共0条评论，按钮，双击切换标签'}]
        screens=[home,profile,post]
        index=[0]; actions=[]
        def action(a,serial=None,timeout=90):
            actions.append(a)
            if a['type']=='tap_xy': index[0]+=1
            return {'ok':True}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(adapter,'status',return_value={'ready':True,'bound':'dev','devices':[{'serial':'dev'}],
                                                               'foreground':'com.xingin.xhs/.MainActivity'}), \
             mock.patch.object(adapter,'harness_read',side_effect=lambda *a,**kw:{'ok':True,'geometry':{'width':1440,'height':3000,'orientation':0},'nodes':screens[index[0]]}), \
             mock.patch.object(adapter,'run_action',side_effect=action), \
             mock.patch.object(read_post,'_jev_choice') as chooser:
            out=read_post.execute(Store(Path(d)),{'device_id':'dev','title':'Test note','author':'Owner'})
            self.assertEqual(out.status,'completed')
            finished = [e for e in Store(Path(d)).read_events(out.run_id) if e['event'] == 'task_finished']
            self.assertEqual(finished[-1]['status'], 'completed')
            self.assertTrue(finished[-1]['ok'])
            self.assertEqual(out.data['coverage'],'visible_only')
            self.assertEqual(out.data['timing']['observations'],3)
            self.assertTrue(all(a['type'] in ('launch_app','tap_xy','scroll') for a in actions))
            self.assertNotIn('launch_app',[a['type'] for a in actions])
            self.assertTrue(all(a.get('effect_class')=='navigation' for a in actions))
            self.assertFalse(out.data['content_retained'])
            self.assertNotIn('evidence_path', out.data)
            persisted = ''.join(p.read_text() for p in Path(d).rglob('*.json'))
            self.assertNotIn('Body text', persisted)
            self.assertNotIn('Test note', persisted)
            chooser.assert_not_called()  # unique grounded matches never need Jev

    def test_ambiguous_target_does_not_click(self):
        from journey_pilot import read_post
        nodes=[{'text':'','desc':'我','clickable':True,'x':x,'y':200} for x in (100,200)]
        with self.assertRaises(read_post.RouteBlocked):
            read_post.select(nodes,lambda n:n.get('desc')=='我')

class CenterCoordinates(unittest.TestCase):
    def test_grounded_coordinate_tap_does_not_reread_tree(self):
        taps=[]
        def no_read():
            raise AssertionError('coordinate action unexpectedly reread the tree')
        script=adapter.build_action_script({'type':'tap_xy','x':100,'y':200,
                                            'locator':{'text':'provenance only'}})
        with contextlib.redirect_stdout(io.StringIO()):
            exec(script,{'ui':no_read,'tap':lambda x,y:taps.append((x,y))})
        self.assertEqual(taps,[(100,200)])

    def test_tap_and_input_use_tree_center(self):
        node = {'text':'target','x':100,'y':200,'w':80,'h':60}
        for kind in ('tap','input_text'):
            with self.subTest(kind=kind):
                taps=[]
                namespace={'ui':lambda:[node],'tap':lambda x,y:taps.append((x,y)),
                           'wait_stable':lambda:None,'type_text':lambda text:None}
                script=adapter.build_action_script({'type':kind,'locator':{'text':'target'},'text':'abc'})
                with contextlib.redirect_stdout(io.StringIO()):
                    exec(script,namespace)
                self.assertEqual(taps,[(100,200)])
