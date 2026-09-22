"""Legacy ancestry proof: private fixtures and owned Darwin process/socket checks."""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import execution_legacy_runner as legacy


class LegacyRunnerProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'install';self.state=self.base/'state'
        self.release=self.root/'releases/0.1.26-beta.29'
        for p in (self.root,self.root/'releases',self.release,self.state,self.state/'admin',self.release/'.venv',self.release/'.venv/bin'):p.mkdir(mode=0o700)
        for name,data in [('VERSION','0.1.26-beta.29\n'),('agent_server.py','# old server\n'),('update_runner.py','# old updater\n'),('release-public-key.pem','public key\n')]:
            (self.release/name).write_text(data);(self.release/name).chmod(0o600)
        self.python=self.release/'.venv/bin/python';self.python.write_text('#!owned\n');self.python.chmod(0o700)
        (self.root/'current').symlink_to(self.release,target_is_directory=True)
        self.identifier='a'*32
        self.status={'update_id':self.identifier,'phase':'installing','target_version':'1.0.4-beta.13','track':'beta'}
        self.health={'server_identity':'owned-identity','server_instance_id':'owned-boot','server_version':'0.1.26-beta.29','active_count':0,'queued':{},'update_service_cgroup':{'safe':True,'unknown_descendant_count':0}}
        self.admitted={**self.status,'server_identity':'owned-identity','server_instance_id':'owned-boot'}
        self.args=argparse.Namespace(platform='Darwin',managed_update_id='',root=str(self.root),state_root=str(self.state),release_version='1.0.4-beta.13',port=7850,bind='127.0.0.1',expected_native_pid=321,expected_server_identity='owned-identity',health_file=str(self.state/'admin/health.json'),update_file=str(self.state/'admin/update.json'))
        self.write(self.state/'admin/server-update.json',self.status);self.write(Path(self.args.health_file),self.health);self.write(Path(self.args.update_file),self.admitted)
        options={'--status-file':str(self.state/'admin/server-update.json'),'--public-key':str(self.root/'current/release-public-key.pem'),'--port':'7850','--bind':'127.0.0.1','--expected-version':'1.0.4-beta.13','--current-version':'0.1.26-beta.29','--track':'beta','--update-id':self.identifier,'--expected-server-identity':'owned-identity','--auth-token-file':str(self.state/'admin'/f'.server-update-{self.identifier}.auth.json')}
        argv=(str(self.root/'current/.venv/bin/python'),str(self.root/'current/update_runner.py'),*(value for pair in options.items() for value in pair))
        def process(pid,parent,exe,argv):return legacy.Process(pid,parent,os.getuid(),os.getuid(),(123456,pid),str(exe),tuple(argv))
        self.runner=process(222,111,self.python,argv)
        self.pane_process=process(111,100,'/bin/zsh',['/bin/zsh','-c','owned'])
        self.tmux_process=process(100,1,'/owned/tmux',['tmux: server'])
        self.child=process(333,222,self.python,[str(self.python),'/owned/helper.py'])
        self.native=process(321,1,self.python,[str(self.python),str(self.root/'current/agent_server.py'),'serve','--bind','127.0.0.1','--port','7850'])
        self.chain=(self.child,self.runner,self.pane_process,self.tmux_process)
        self.pane=('agents_server_update_'+self.identifier,'$1','%1',111,self.tmux_process,self.runner.argv,(1,2,os.getuid(),0o140600))
        self.enterContext(mock.patch.object(legacy.sys,'platform','darwin'))
        self.proc=self.enterContext(mock.patch.object(legacy,'_process',return_value=self.native))
        self.ancestry=self.enterContext(mock.patch.object(legacy,'_chain',side_effect=lambda _pid:self.chain))
        self.tmux=self.enterContext(mock.patch.object(legacy,'_tmux',side_effect=lambda _name:self.pane))

    def write(self,path,value):path.write_text(json.dumps(value));path.chmod(0o600)
    def verify(self):return legacy.verify_legacy_runner(self.args,self.status)
    def runner_args(self,argv):
        self.runner=replace(self.runner,argv=tuple(argv));self.chain=(self.child,self.runner,self.pane_process,self.tmux_process)
        self.pane=(*self.pane[:5],self.runner.argv,self.pane[6])

    def test_exact_owner_is_read_only_and_rechecked(self):
        before={p:p.read_bytes() for p in (self.state/'admin').iterdir()}
        self.assertEqual(self.verify(),self.identifier)
        self.assertEqual(before,{p:p.read_bytes() for p in (self.state/'admin').iterdir()})
        self.assertEqual(self.ancestry.call_count,2);self.assertEqual(self.tmux.call_count,2)

    def test_fallback_never_reinterprets_present_or_malformed_ownership(self):
        for value in (None,0,'',222,'222',False):
            with self.subTest(value=value),self.assertRaisesRegex(RuntimeError,'does not apply'):legacy.verify_legacy_runner(self.args,{**self.status,'runner_pid':value})
        self.args.managed_update_id='foreign'
        with self.assertRaisesRegex(RuntimeError,'does not apply'):self.verify()
        self.args.managed_update_id='';self.args.platform='Linux'
        with self.assertRaisesRegex(RuntimeError,'does not apply'):self.verify()

    def test_wrong_native_process_or_listener_is_refused(self):
        for value in (replace(self.native,executable='/foreign/python'),replace(self.native,argv=(*self.native.argv[:-1],'9999'))):
            self.proc.return_value=value
            with self.assertRaises(RuntimeError):self.verify()

    def framework(self):
        version=self.base/'Python.framework/Versions/3.14'
        launcher=version/'bin/python3.14';companion=version/'Resources/Python.app/Contents/MacOS/Python'
        for executable in (launcher,companion):
            executable.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            executable.write_text('#!owned\n');executable.chmod(0o700)
        self.python.unlink();self.python.symlink_to(launcher)
        original_launch=self.runner.argv
        self.native=replace(self.native,executable=str(companion),argv=(str(companion),*self.native.argv[1:]))
        self.proc.return_value=self.native
        self.runner=replace(self.runner,executable=str(companion),argv=(str(companion),*self.runner.argv[1:]))
        self.chain=(self.child,self.runner,self.pane_process,self.tmux_process)
        self.pane=(*self.pane[:5],original_launch,self.pane[6])
        return launcher,companion

    def test_framework_launcher_accepts_only_bound_same_version_companion(self):
        launcher,companion=self.framework()
        self.assertEqual(self.verify(),self.identifier)
        for executable in (launcher,companion.with_name('OtherPython'),self.base/'Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python'):
            self.runner=replace(self.runner,executable=str(executable))
            self.chain=(self.child,self.runner,self.pane_process,self.tmux_process)
            with self.subTest(executable=executable),self.assertRaisesRegex(RuntimeError,'not unique'):self.verify()

    def test_framework_files_and_directories_must_be_safe_and_unchanged(self):
        launcher,companion=self.framework()
        for path in (launcher,companion,companion.parent,launcher.parents[3]):
            mode=path.stat().st_mode & 0o777;path.chmod(mode|0o020)
            with self.subTest(path=path),self.assertRaisesRegex(RuntimeError,'unsafe'):self.verify()
            path.chmod(mode)
        calls=0
        def replace_companion(_):
            nonlocal calls
            calls+=1
            if calls==2:
                replacement=companion.with_name('replacement');replacement.write_text('#!replacement\n');replacement.chmod(0o700);os.replace(replacement,companion)
            return self.pane
        self.tmux.side_effect=replace_companion
        with self.assertRaisesRegex(RuntimeError,'runtime changed'):self.verify()

    def test_target_identity_and_operation_pins_cannot_change(self):
        for key,value in [('update_id','b'*32),('target_version','1.0.4-beta.9'),('track','stable'),('phase','failed'),('runner_pid',None)]:
            self.write(self.state/'admin/server-update.json',{**self.status,key:value})
            with self.subTest(key=key),self.assertRaises(RuntimeError):self.verify()
        self.write(self.state/'admin/server-update.json',self.status)
        for key,value in [('server_identity','foreign'),('server_instance_id','foreign'),('active_count',1),('queued',{'chat':1}),('update_service_cgroup',{'safe':False,'unknown_descendant_count':1})]:
            self.write(Path(self.args.health_file),{**self.health,key:value})
            with self.subTest(key=key),self.assertRaises(RuntimeError):self.verify()

    def test_runner_arguments_require_exact_values_and_unique_options(self):
        original=self.runner.argv
        for key in ('--update-id','--status-file','--public-key','--expected-version','--current-version','--track','--port','--bind','--expected-server-identity','--auth-token-file'):
            argv=list(original);argv[argv.index(key)+1]='foreign';self.runner_args(argv)
            with self.subTest(key=key),self.assertRaises((RuntimeError,OSError)):self.verify()
        for suffix in (('--update-id',self.identifier),('--unrecognized','value'),('--update-id='+self.identifier,),('--track',)):
            self.runner_args((*original,*suffix))
            with self.subTest(suffix=suffix),self.assertRaises(RuntimeError):self.verify()

    def test_runner_must_be_unique_ancestor_in_exact_tmux_pane(self):
        original=self.chain
        for chain in ((self.child,self.pane_process,self.tmux_process),(*original[:2],replace(self.runner,pid=223),*original[2:])):
            self.chain=chain
            with self.assertRaisesRegex(RuntimeError,'not unique'):self.verify()
        self.chain=original;self.pane=(*self.pane[:3],999,*self.pane[4:])
        with self.assertRaisesRegex(RuntimeError,'outside'):self.verify()

    def test_tmux_launch_must_match_not_merely_contain_runner(self):
        self.pane=(*self.pane[:5],('/bin/sh','-c',shlex.join(self.runner.argv)),self.pane[6])
        with self.assertRaisesRegex(RuntimeError,'launch command'):self.verify()

    def test_serialized_tmux_launch_keeps_exact_kernel_argument_check(self):
        command=shlex.join(self.runner.argv)
        for display in (command,json.dumps(command)):
            self.pane=(*self.pane[:5],legacy._launch_arguments(display),self.pane[6])
            self.assertEqual(self.verify(),self.identifier)
        for command in (shlex.join((*self.runner.argv,'--extra','value')),
                        shlex.join(self.runner.argv)+'; /bin/true',
                        shlex.join(self.runner.argv)+' && /bin/true'):
            self.pane=(*self.pane[:5],legacy._launch_arguments(json.dumps(command)),self.pane[6])
            with self.subTest(command=command),self.assertRaisesRegex(RuntimeError,'launch command'):self.verify()

    def test_pid_reuse_and_reparenting_are_detected(self):
        for changed in (replace(self.runner,started=(123457,222)),replace(self.runner,ppid=777)):
            self.ancestry.side_effect=[self.chain,(self.child,changed,self.pane_process,self.tmux_process)]
            with self.assertRaisesRegex(RuntimeError,'changed during'):self.verify()

    def test_heartbeat_allowed_but_new_operation_is_not(self):
        calls=0
        def heartbeat(_):
            nonlocal calls
            calls+=1;self.write(self.state/'admin/server-update.json',{**self.status,'updated_at':str(calls)});return self.pane
        self.tmux.side_effect=heartbeat;self.assertEqual(self.verify(),self.identifier)
        def replacement(_):
            self.write(self.state/'admin/server-update.json',{**self.status,'update_id':'b'*32});return self.pane
        self.tmux.side_effect=replacement
        with self.assertRaisesRegex(RuntimeError,'changed'):self.verify()

    def test_source_and_current_link_replacement_are_refused(self):
        calls=0
        def rewrite(_):
            nonlocal calls
            calls+=1
            if calls==2:(self.release/'update_runner.py').write_text('# replaced\n')
            return self.pane
        self.tmux.side_effect=rewrite
        with self.assertRaisesRegex(RuntimeError,'runtime changed'):self.verify()
        (self.root/'current').unlink();(self.root/'current').mkdir()
        with self.assertRaisesRegex(RuntimeError,'owned link'):self.verify()

    def test_identical_proof_file_replacement_is_refused(self):
        calls=0
        def replace_file(_):
            nonlocal calls
            calls+=1
            if calls==2:
                path=Path(self.args.health_file);replacement=path.with_name('replacement.json')
                self.write(replacement,self.health);os.replace(replacement,path)
            return self.pane
        self.tmux.side_effect=replace_file
        with self.assertRaisesRegex(RuntimeError,'changed during'):self.verify()

    def test_changed_pane_identity_is_refused(self):
        self.tmux.side_effect=[self.pane,(*self.pane[:2],'%2',*self.pane[3:])]
        with self.assertRaisesRegex(RuntimeError,'changed during'):self.verify()

    def test_private_proof_links_duplicates_and_unsafe_modes_are_refused(self):
        proof=Path(self.args.health_file);proof.chmod(0o644)
        with self.assertRaises(RuntimeError):self.verify()
        proof.chmod(0o600);proof.write_text('{"active_count":0,"active_count":0}')
        with self.assertRaisesRegex(ValueError,'duplicate'):self.verify()
        proof.unlink();proof.symlink_to(Path(self.args.update_file))
        with self.assertRaises(RuntimeError):self.verify()


class KernelArgumentsTests(unittest.TestCase):
    def raw(self,args,trailer=b'PRIVATE_ENV=not-an-argument\0'):
        return struct.pack('=i',len(args))+b'/owned/python\0\0\0'+b'\0'.join(x.encode() for x in args)+b'\0'+trailer

    def test_spaces_empty_arguments_and_environment_boundary(self):
        argv=('/owned/python','/a path/runner.py','--url','','--value','single quote\' and "double"')
        self.assertEqual(legacy._arguments(self.raw(argv)),argv)

    def test_tmux_single_shell_argument_has_only_one_serialization_layer(self):
        argv=('/owned/python','/a path/runner.py','--url','','--value','single quote\' and "double"')
        command=shlex.join(argv)
        self.assertEqual(legacy._launch_arguments(command),argv)
        self.assertEqual(legacy._launch_arguments(json.dumps(command)),argv)
        for display in (json.dumps(json.dumps(command)),command+'\n/bin/true',command+'\x7f'):
            with self.subTest(display=display),self.assertRaises(RuntimeError):legacy._launch_arguments(display)

    def test_malformed_kernel_data_is_rejected(self):
        for raw in (b'',struct.pack('=i',0)+b'/p\0',struct.pack('=i',99999)+b'/p\0',self.raw(('p','x'))[:-len(b'PRIVATE_ENV=not-an-argument\0')-1],self.raw(('p','bad\nvalue'))):
            with self.subTest(raw=raw[:20]),self.assertRaises((RuntimeError,UnicodeError)):legacy._arguments(raw)

    def test_kernel_foreign_uid_is_rejected_before_arguments_read(self):
        import ctypes
        from types import SimpleNamespace
        def info(_pid,_kind,_arg,pointer,_size):
            value=ctypes.cast(pointer,ctypes.POINTER(legacy._BSDInfo)).contents
            value.pid=222;value.ppid=111;value.uid=os.getuid()+1;value.ruid=os.getuid();value.start_sec=1
            return ctypes.sizeof(value)
        library=SimpleNamespace(proc_pidinfo=mock.Mock(side_effect=info),proc_pidpath=mock.Mock())
        with mock.patch.object(legacy.sys,'platform','darwin'),mock.patch.object(legacy.ctypes,'CDLL',return_value=library):
            with self.assertRaisesRegex(RuntimeError,'owner or incarnation'):legacy._process(222)
        library.proc_pidpath.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin','requires Darwin kernel process APIs')
    def test_real_owned_process_arguments_and_incarnation(self):
        # Launch the actual executable, through a venv-like symlink. Homebrew's
        # framework launcher re-execs a different binary; this test isolates
        # exact kernel parsing from that separately checked interpreter alias.
        executable=Path(legacy._process(os.getpid()).executable)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();interpreter=root/'python';interpreter.symlink_to(executable)
            ready=root/'ready'
            code='from pathlib import Path; import time; Path('+repr(str(ready))+').write_text("ready"); time.sleep(30)'
            argv=(str(interpreter),'-c',code,'space value','',"a'b")
            process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'owned Python child did not start')
                first=legacy._process(process.pid);second=legacy._process(process.pid)
                self.assertEqual(first,second);self.assertEqual(first.argv,argv)
                self.assertEqual((first.uid,first.ruid),(os.getuid(),os.getuid()));self.assertEqual(first.ppid,os.getpid())
                self.assertEqual(Path(first.executable).resolve(),interpreter.resolve())
            finally:process.terminate();process.wait(timeout=10)

    @unittest.skipUnless(sys.platform=='darwin' and shutil.which('tmux'),'requires Darwin and tmux')
    def test_real_owned_tmux_socket_and_pane_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();root.chmod(0o700);socket=root/'tmux.sock';binary=Path(shutil.which('tmux')).resolve()
            ready=root/'ready'
            code='from pathlib import Path; import time; Path('+repr(str(ready))+').write_text("ready"); time.sleep(30)'
            args=(legacy._process(os.getpid()).executable,'-c',code,'space value','',"a'b");session='agents_server_update_'+'a'*32
            env={'PATH':'/usr/bin:/bin','TERM':'xterm-256color'}
            if os.environ.get('TERMINFO'):env['TERMINFO']=os.environ['TERMINFO']
            started=subprocess.run([str(binary),'-S',str(socket),'-f','/dev/null','new-session','-d','-s',session,shlex.join(args)],capture_output=True,env=env,timeout=10)
            self.assertEqual(started.returncode,0,started.stderr.decode())
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'owned pane child did not start')
                pane=legacy._tmux_at(session,socket,binary);self.assertEqual(pane[5],args);self.assertEqual(legacy._process(pane[3]).argv,args)
                extra=subprocess.run([str(binary),'-S',str(socket),'split-window','-d','-t',pane[2],'sleep 30'],capture_output=True,env=env,timeout=10)
                self.assertEqual(extra.returncode,0,extra.stderr.decode())
                with self.assertRaisesRegex(RuntimeError,'one pane'):legacy._tmux_at(session,socket,binary)
            finally:subprocess.run([str(binary),'-S',str(socket),'kill-server'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,timeout=10,check=False)

    @unittest.skipUnless(sys.platform=='darwin','requires Darwin framework Python')
    def test_real_framework_venv_launcher_and_kernel_companion(self):
        base=Path(getattr(sys,'_base_executable',sys.executable)).resolve()
        paths,_=legacy._python_proof(base)
        if len(paths)!=2:self.skipTest('test interpreter does not use a Python.framework launcher')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();venv=root/'venv'
            subprocess.run([str(base),'-m','venv','--without-pip',str(venv)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            interpreter=venv/'bin/python';proof=legacy._python_proof(interpreter)
            ready=root/'ready';code='from pathlib import Path; import time; Path('+repr(str(ready))+').write_text("ready"); time.sleep(30)'
            process=subprocess.Popen([str(interpreter),'-c',code,'space value',''],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'owned framework child did not start')
                observed=legacy._process(process.pid)
                self.assertEqual(Path(observed.executable),proof[0][1]);self.assertEqual(Path(observed.argv[0]),proof[0][1])
                self.assertEqual(observed.argv[1:],('-c',code,'space value',''))
                self.assertEqual(legacy._python_proof(interpreter),proof)
            finally:process.terminate();process.wait(timeout=10)


if __name__=='__main__':unittest.main()
