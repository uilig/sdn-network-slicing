#!/usr/bin/env python3
"""
Script di test completo per il SDN Network Slicing.
Usa l'API Python di Mininet (senza CLI interattiva) per eseguire
test automatizzati su tutti e tre i controller.
"""

import subprocess
import sys
import os
import time
import signal
import json
from datetime import datetime

# Aggiungi il path di Mininet
sys.path.insert(0, '/usr/lib/python3/dist-packages')

from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(PROJECT_DIR, 'test_results')
os.makedirs(RESULTS_DIR, exist_ok=True)

# Importa la topologia dal progetto
sys.path.insert(0, PROJECT_DIR)
from topology import PremiumLinkTopology

# Colori
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

def log(msg, color=RESET):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"{color}[{ts}] {msg}{RESET}", flush=True)

def save(filename, content):
    with open(os.path.join(RESULTS_DIR, filename), 'w') as f:
        f.write(content)

def cleanup():
    log("Cleanup...", YELLOW)
    os.system('mn -c > /dev/null 2>&1')
    os.system('pkill -9 -f ryu-manager > /dev/null 2>&1')
    time.sleep(3)

def start_controller(name):
    path = os.path.join(PROJECT_DIR, name)
    logfile = os.path.join(RESULTS_DIR, f'log_{name.replace(".py","")}.txt')
    log(f"Avvio controller: {name}", CYAN)
    # Il controller gira come utente normale (ryu è installato in user-space)
    # sudo non vede i pacchetti pip dell'utente, quindi usiamo sudo -u
    import getpass
    current_user = os.environ.get('SUDO_USER', getpass.getuser())
    ryu_bin = f'/home/{current_user}/.local/bin/ryu-manager'
    proc = subprocess.Popen(
        ['sudo', '-u', current_user, ryu_bin, path],
        stdout=open(logfile, 'w'),
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid
    )
    time.sleep(6)
    if proc.poll() is not None:
        log(f"ERRORE: controller terminato (exit={proc.returncode})", RED)
        with open(logfile) as f:
            log(f.read()[-500:], RED)
        return None
    log(f"Controller OK (PID {proc.pid})", GREEN)
    return proc

def start_net():
    log("Avvio topologia Mininet...", CYAN)
    topo = PremiumLinkTopology()
    net = Mininet(
        topo=topo,
        switch=OVSKernelSwitch,
        controller=RemoteController('c0', ip='127.0.0.1', port=6653),
        link=TCLink,
        autoSetMacs=False
    )
    net.start()
    log("Attesa connessione switch al controller (10s)...", YELLOW)
    time.sleep(10)
    log("Topologia pronta", GREEN)
    return net

def stop_all(ctrl, net):
    if net:
        try:
            net.stop()
        except:
            pass
    if ctrl and ctrl.poll() is None:
        try:
            os.killpg(os.getpgid(ctrl.pid), signal.SIGTERM)
        except:
            pass
    time.sleep(2)
    os.system('mn -c > /dev/null 2>&1')
    os.system('pkill -9 -f ryu-manager > /dev/null 2>&1')
    time.sleep(2)

def host_cmd(net, hostname, cmd, timeout=30):
    """Esegue un comando su un host Mininet e restituisce l'output."""
    host = net.get(hostname)
    log(f"  {hostname}> {cmd}", RESET)
    output = host.cmd(cmd, timeout=timeout) if hasattr(host.cmd, '__call__') else ''
    # Fallback: popen
    if not output:
        try:
            result = host.popen(cmd, shell=True)
            output, _ = result.communicate(timeout=timeout)
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
        except:
            output = "[timeout o errore]"
    return output.strip()

def get_flows(switch_name):
    """Dump delle flow table di uno switch."""
    result = subprocess.run(
        ['sudo', 'ovs-ofctl', 'dump-flows', switch_name, '--protocols=OpenFlow13'],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout

def run_cmd_system(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except:
        return "[errore]"


# =====================================================================
#  TEST 1: TOPOLOGY SLICING
# =====================================================================
def test_topology_slicing():
    log("=" * 65, BOLD)
    log("  TEST 1: TOPOLOGY SLICING CONTROLLER", BOLD)
    log("=" * 65, BOLD)
    output_all = []

    cleanup()
    ctrl = start_controller('topology_slicing_controller.py')
    if not ctrl:
        return {'status': 'ERRORE'}

    net = start_net()

    # --- Test 1.1: pingall ---
    log("Test 1.1: pingall (atteso: 4/12 = 66% drop)", CYAN)
    pingall_result = net.pingAll(timeout=5)
    # pingAll ritorna la percentuale di drop
    output_all.append(f"=== PINGALL ===\nDrop rate: {pingall_result}%\n")
    log(f"  Risultato pingall: {pingall_result}% drop", GREEN if pingall_result > 50 else RED)

    # --- Test 1.2: ping intra-slice H1 -> H3 ---
    log("Test 1.2: ping H1 -> H3 (intra-slice upper, deve funzionare)", CYAN)
    h1 = net.get('H1')
    ping_h1_h3 = h1.cmd('ping -c 5 10.0.0.3')
    output_all.append(f"\n=== PING H1 -> H3 (intra-slice) ===\n{ping_h1_h3}\n")
    success = '0% packet loss' in ping_h1_h3
    log(f"  H1->H3: {'OK' if success else 'FAIL'}", GREEN if success else RED)

    # --- Test 1.3: ping intra-slice H2 -> H4 ---
    log("Test 1.3: ping H2 -> H4 (intra-slice lower, deve funzionare)", CYAN)
    h2 = net.get('H2')
    ping_h2_h4 = h2.cmd('ping -c 5 10.0.0.4')
    output_all.append(f"\n=== PING H2 -> H4 (intra-slice) ===\n{ping_h2_h4}\n")
    success = '0% packet loss' in ping_h2_h4
    log(f"  H2->H4: {'OK' if success else 'FAIL'}", GREEN if success else RED)

    # --- Test 1.4: ping cross-slice H1 -> H4 (deve fallire) ---
    log("Test 1.4: ping H1 -> H4 (cross-slice, deve fallire)", CYAN)
    ping_h1_h4 = h1.cmd('ping -c 3 -W 2 10.0.0.4')
    output_all.append(f"\n=== PING H1 -> H4 (cross-slice) ===\n{ping_h1_h4}\n")
    blocked = '100% packet loss' in ping_h1_h4 or 'Unreachable' in ping_h1_h4
    log(f"  H1->H4 bloccato: {'OK' if blocked else 'FAIL'}", GREEN if blocked else RED)

    # --- Test 1.5: ping cross-slice H2 -> H3 (deve fallire) ---
    log("Test 1.5: ping H2 -> H3 (cross-slice, deve fallire)", CYAN)
    ping_h2_h3 = h2.cmd('ping -c 3 -W 2 10.0.0.3')
    output_all.append(f"\n=== PING H2 -> H3 (cross-slice) ===\n{ping_h2_h3}\n")
    blocked = '100% packet loss' in ping_h2_h3 or 'Unreachable' in ping_h2_h3
    log(f"  H2->H3 bloccato: {'OK' if blocked else 'FAIL'}", GREEN if blocked else RED)

    # --- Test 1.6: Flow tables ---
    log("Test 1.6: Dump flow tables S1, S2, S4, S6", CYAN)
    flows_output = ""
    for sw in ['s1', 's2', 's4', 's6']:
        flows = get_flows(sw)
        flows_output += f"\n=== FLOWS {sw.upper()} ===\n{flows}\n"
        n_rules = len([l for l in flows.splitlines() if 'cookie' in l])
        log(f"  {sw.upper()}: {n_rules} regole", GREEN)
    output_all.append(flows_output)

    # Salva tutto
    full_output = '\n'.join(output_all)
    save('test1_topology_slicing.txt', full_output)
    log(f"Risultati salvati in test_results/test1_topology_slicing.txt", GREEN)

    # Leggi log controller
    log_path = os.path.join(RESULTS_DIR, 'log_topology_slicing_controller.txt')
    if os.path.exists(log_path):
        with open(log_path) as f:
            ctrl_log = f.read()
        blocked_count = ctrl_log.count('BLOCCATO')
        log(f"Log controller: {len(ctrl_log.splitlines())} righe, {blocked_count} pacchetti cross-slice bloccati", GREEN)
        save('test1_controller_log.txt', ctrl_log)

    stop_all(ctrl, net)
    log("Test 1 COMPLETATO", GREEN)
    return {'status': 'OK', 'pingall_drop': pingall_result}


# =====================================================================
#  TEST 2: SERVICE SLICING
# =====================================================================
def test_service_slicing():
    log("=" * 65, BOLD)
    log("  TEST 2: SERVICE SLICING CONTROLLER", BOLD)
    log("=" * 65, BOLD)
    output_all = []

    cleanup()
    ctrl = start_controller('service_slicing_controller.py')
    if not ctrl:
        return {'status': 'ERRORE'}

    net = start_net()

    # --- Test 2.1: pingall ---
    log("Test 2.1: pingall (atteso: 12/12 = 0% drop)", CYAN)
    pingall_result = net.pingAll(timeout=5)
    output_all.append(f"=== PINGALL ===\nDrop rate: {pingall_result}%\n")
    log(f"  Risultato pingall: {pingall_result}% drop", GREEN if pingall_result == 0 else RED)

    h1 = net.get('H1')
    h2 = net.get('H2')
    h3 = net.get('H3')
    h4 = net.get('H4')

    # --- Test 2.2: iperf video UDP 9999 upper path (H1->H3) ---
    log("Test 2.2: iperf video UDP 9999 H1->H3 (premium link ~5-6 Mbps)", CYAN)
    h3.cmd('iperf -s -u -p 9999 &')
    time.sleep(1)
    video_upper = h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 10M -t 10')
    output_all.append(f"\n=== IPERF VIDEO H1->H3 (UDP 9999, premium) ===\n{video_upper}\n")
    log(f"  Video H1->H3:\n{video_upper}", GREEN)

    # --- Test 2.3: iperf standard TCP upper path (H1->H3) ---
    log("Test 2.3: iperf standard TCP H1->H3 (percorso standard ~1.5-2 Mbps)", CYAN)
    h3.cmd('iperf -s -p 5001 &')
    time.sleep(1)
    std_upper = h1.cmd('iperf -c 10.0.0.3 -p 5001 -t 10')
    output_all.append(f"\n=== IPERF STANDARD H1->H3 (TCP 5001, standard) ===\n{std_upper}\n")
    log(f"  Standard H1->H3:\n{std_upper}", GREEN)

    # --- Test 2.4: iperf video UDP 9999 lower path (H2->H4) ---
    log("Test 2.4: iperf video UDP 9999 H2->H4 (premium link inferiore)", CYAN)
    h4.cmd('iperf -s -u -p 9999 &')
    time.sleep(1)
    video_lower = h2.cmd('iperf -c 10.0.0.4 -u -p 9999 -b 10M -t 10')
    output_all.append(f"\n=== IPERF VIDEO H2->H4 (UDP 9999, premium) ===\n{video_lower}\n")
    log(f"  Video H2->H4:\n{video_lower}", GREEN)

    # --- Test 2.5: iperf standard TCP lower path (H2->H4) ---
    log("Test 2.5: iperf standard TCP H2->H4 (percorso standard)", CYAN)
    h4.cmd('iperf -s -p 5001 &')
    time.sleep(1)
    std_lower = h2.cmd('iperf -c 10.0.0.4 -p 5001 -t 10')
    output_all.append(f"\n=== IPERF STANDARD H2->H4 (TCP 5001, standard) ===\n{std_lower}\n")
    log(f"  Standard H2->H4:\n{std_lower}", GREEN)

    # --- Test 2.6: ping con RTT (H1->H3 premium vs standard) ---
    log("Test 2.6: ping H1->H3 per RTT", CYAN)
    ping_rtt = h1.cmd('ping -c 10 10.0.0.3')
    output_all.append(f"\n=== PING H1->H3 (RTT) ===\n{ping_rtt}\n")
    log(f"  RTT H1->H3:\n{ping_rtt}", GREEN)

    # --- Test 2.7: Flow tables ---
    log("Test 2.7: Dump flow tables S2, S4, S6", CYAN)
    flows_output = ""
    for sw in ['s1', 's2', 's3', 's4', 's5', 's6']:
        flows = get_flows(sw)
        flows_output += f"\n=== FLOWS {sw.upper()} ===\n{flows}\n"
        n_rules = len([l for l in flows.splitlines() if 'cookie' in l])
        log(f"  {sw.upper()}: {n_rules} regole", GREEN)
    output_all.append(flows_output)

    # Kill iperf servers
    h3.cmd('kill %iperf 2>/dev/null')
    h4.cmd('kill %iperf 2>/dev/null')

    full_output = '\n'.join(output_all)
    save('test2_service_slicing.txt', full_output)
    log(f"Risultati salvati in test_results/test2_service_slicing.txt", GREEN)

    stop_all(ctrl, net)
    log("Test 2 COMPLETATO", GREEN)
    return {'status': 'OK', 'pingall_drop': pingall_result}


# =====================================================================
#  TEST 4: STRESS TEST (VIDEO + TCP + PING SIMULTANEI)
# =====================================================================
def test_stress():
    log("=" * 65, BOLD)
    log("  TEST 4: STRESS TEST (VIDEO + TCP + PING SIMULTANEI)", BOLD)
    log("=" * 65, BOLD)
    output_all = []

    cleanup()
    ctrl = start_controller('service_slicing_controller.py')
    if not ctrl:
        return {'status': 'ERRORE'}

    net = start_net()

    h1 = net.get('H1')
    h3 = net.get('H3')

    # ---- Scenario A: Video + TCP + Ping simultanei ----
    log("Scenario A: Video + TCP + Ping simultanei H1->H3", CYAN)
    output_all.append("=" * 60)
    output_all.append("SCENARIO A: Video UDP 9999 + TCP 5001 + Ping simultanei")
    output_all.append("=" * 60)

    # Avvio server su H3
    h3.cmd('iperf -s -u -p 9999 &')
    h3.cmd('iperf -s -p 5001 &')
    time.sleep(2)

    # Avvio client in background
    h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 4M -t 20 > /tmp/stress_video_a.txt 2>&1 &')
    h1.cmd('iperf -c 10.0.0.3 -p 5001 -t 18 > /tmp/stress_tcp_a.txt 2>&1 &')
    log("  Video UDP e TCP avviati in background. Ping in foreground...", YELLOW)

    # Ping in foreground (~15s)
    ping_a = h1.cmd('ping -c 15 10.0.0.3')
    output_all.append(f"\n--- PING (Scenario A) ---\n{ping_a}")

    # Attesa fine iperf
    time.sleep(8)

    # Raccolta risultati
    video_a = h1.cmd('cat /tmp/stress_video_a.txt')
    tcp_a = h1.cmd('cat /tmp/stress_tcp_a.txt')
    output_all.append(f"\n--- IPERF VIDEO UDP 9999 (Scenario A) ---\n{video_a}")
    output_all.append(f"\n--- IPERF TCP 5001 (Scenario A) ---\n{tcp_a}")

    log(f"  Video A:\n{video_a}", GREEN)
    log(f"  TCP A:\n{tcp_a}", GREEN)
    log(f"  Ping A:\n{ping_a}", GREEN)

    # Kill server
    h3.cmd('killall iperf 2>/dev/null')
    h1.cmd('killall iperf 2>/dev/null')
    time.sleep(3)

    # ---- Scenario B: Solo Video + Ping (no TCP) ----
    log("Scenario B: Solo Video + Ping H1->H3 (no TCP)", CYAN)
    output_all.append("\n" + "=" * 60)
    output_all.append("SCENARIO B: Solo Video UDP 9999 + Ping (senza TCP)")
    output_all.append("=" * 60)

    # Avvio server su H3
    h3.cmd('iperf -s -u -p 9999 &')
    time.sleep(2)

    # Avvio video in background
    h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 4M -t 20 > /tmp/stress_video_b.txt 2>&1 &')
    log("  Video UDP avviato in background. Ping in foreground...", YELLOW)

    # Ping in foreground
    ping_b = h1.cmd('ping -c 15 10.0.0.3')
    output_all.append(f"\n--- PING (Scenario B) ---\n{ping_b}")

    # Attesa fine iperf
    time.sleep(8)

    video_b = h1.cmd('cat /tmp/stress_video_b.txt')
    output_all.append(f"\n--- IPERF VIDEO UDP 9999 (Scenario B) ---\n{video_b}")

    log(f"  Video B:\n{video_b}", GREEN)
    log(f"  Ping B:\n{ping_b}", GREEN)

    # Kill server
    h3.cmd('killall iperf 2>/dev/null')
    h1.cmd('killall iperf 2>/dev/null')

    # ---- Analisi confronto ----
    output_all.append("\n" + "=" * 60)
    output_all.append("CONFRONTO SCENARIO A vs B")
    output_all.append("=" * 60)

    def parse_ping_rtt(ping_out):
        for line in ping_out.splitlines():
            if 'rtt min/avg/max' in line:
                parts = line.split('=')[1].strip().split('/')
                return {'min': parts[0], 'avg': parts[1], 'max': parts[2], 'mdev': parts[3].split()[0]}
        return None

    def parse_ping_loss(ping_out):
        for line in ping_out.splitlines():
            if 'packet loss' in line:
                for part in line.split(','):
                    if 'packet loss' in part:
                        return part.strip()
        return 'N/A'

    rtt_a = parse_ping_rtt(ping_a)
    rtt_b = parse_ping_rtt(ping_b)
    loss_a = parse_ping_loss(ping_a)
    loss_b = parse_ping_loss(ping_b)

    summary = []
    summary.append(f"Ping RTT medio Scenario A (con TCP): {rtt_a['avg'] if rtt_a else 'N/A'} ms")
    summary.append(f"Ping RTT medio Scenario B (senza TCP): {rtt_b['avg'] if rtt_b else 'N/A'} ms")
    summary.append(f"Ping packet loss A: {loss_a}")
    summary.append(f"Ping packet loss B: {loss_b}")
    summary.append("")
    summary.append("Atteso: Video ~uguale in A e B (premium link isolato).")
    summary.append("        RTT ping piu alto in A (TCP congestiona percorso standard).")

    for s in summary:
        output_all.append(s)
        log(f"  {s}", GREEN)

    full_output = '\n'.join(output_all)
    save('test4_stress_test.txt', full_output)
    log(f"Risultati salvati in test_results/test4_stress_test.txt", GREEN)

    stop_all(ctrl, net)
    log("Test 4 COMPLETATO", GREEN)
    return {'status': 'OK'}


# =====================================================================
#  TEST 3: DYNAMIC SLICING CON PREEMPTION
# =====================================================================
def test_dynamic_slicing():
    log("=" * 65, BOLD)
    log("  TEST 3: DYNAMIC SLICING + VIDEO PREEMPTION", BOLD)
    log("=" * 65, BOLD)
    output_all = []

    cleanup()
    ctrl = start_controller('dynamic_slicing_controller.py')
    if not ctrl:
        return {'status': 'ERRORE'}

    net = start_net()

    h1 = net.get('H1')
    h3 = net.get('H3')

    # --- Test 3.1: pingall ---
    log("Test 3.1: pingall (atteso: 12/12 = 0% drop)", CYAN)
    pingall_result = net.pingAll(timeout=5)
    output_all.append(f"=== PINGALL ===\nDrop rate: {pingall_result}%\n")
    log(f"  Risultato pingall: {pingall_result}% drop", GREEN if pingall_result == 0 else RED)

    # --- Test 3.2: Avvio traffico dinamico UDP 800 ---
    log("Test 3.2: Avvio traffico dinamico UDP 800 (1 Mbps) H1->H3", CYAN)
    h3.cmd('iperf -s -u -p 800 &')
    time.sleep(1)
    h1.cmd('iperf -c 10.0.0.3 -u -p 800 -b 1M -t 60 &')
    output_all.append(f"\n=== TRAFFICO DINAMICO UDP 800 AVVIATO (1 Mbps, 60s) ===\n")
    log("  Traffico dinamico avviato. Attesa allocazione (15s)...", YELLOW)
    time.sleep(15)

    # --- Test 3.3: Flow table durante allocazione ---
    log("Test 3.3: Flow table S2 durante allocazione dinamica", CYAN)
    flows_alloc = get_flows('s2')
    output_all.append(f"\n=== FLOWS S2 DURANTE ALLOCAZIONE DINAMICA ===\n{flows_alloc}\n")
    has_prio90 = 'priority=110' in flows_alloc
    log(f"  Regola priority=110 presente: {'SI' if has_prio90 else 'NO'}",
        GREEN if has_prio90 else RED)
    save('test3_flows_s2_allocation.txt', flows_alloc)

    # --- Test 3.4: API stats prima del video ---
    log("Test 3.4: Verifica REST API /api/stats", CYAN)
    api1 = run_cmd_system('curl -s http://127.0.0.1:8080/api/stats')
    output_all.append(f"\n=== API STATS (prima del video) ===\n{api1}\n")
    try:
        data1 = json.loads(api1)
        log(f"  Switch connessi: {data1.get('switches_connected', [])}", GREEN)
        log(f"  Upper usage: {data1['premium_links']['upper']['usage_mbps']:.2f} Mbps", GREEN)
        log(f"  Dynamic active upper: {data1['premium_links']['upper']['dynamic_active']}", GREEN)
        log(f"  Video active upper: {data1['premium_links']['upper']['video_active']}", GREEN)
        save('test3_api_before_video.json', json.dumps(data1, indent=2))
    except Exception as e:
        log(f"  API errore: {e}", RED)

    # --- Test 3.5: Avvio video UDP 9999 (triggera preemption) ---
    log("Test 3.5: Avvio video UDP 9999 (4 Mbps) H1->H3 -> PREEMPTION", CYAN)
    h3.cmd('iperf -s -u -p 9999 &')
    time.sleep(1)
    h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 4M -t 25 &')
    output_all.append(f"\n=== VIDEO UDP 9999 AVVIATO (4 Mbps, 25s) ===\n")
    log("  Video avviato. Attesa preemption (10s)...", YELLOW)
    time.sleep(10)

    # --- Test 3.6: Flow table durante preemption ---
    log("Test 3.6: Flow table S2 durante preemption", CYAN)
    flows_preempt = get_flows('s2')
    output_all.append(f"\n=== FLOWS S2 DURANTE PREEMPTION ===\n{flows_preempt}\n")
    has_prio90_after = 'priority=110' in flows_preempt
    log(f"  Regola priority=110 presente: {'SI (FAIL - doveva essere rimossa!)' if has_prio90_after else 'NO (OK - preemption riuscita)'}",
        RED if has_prio90_after else GREEN)
    save('test3_flows_s2_preemption.txt', flows_preempt)

    # --- Test 3.7: API stats durante video ---
    log("Test 3.7: API stats durante video", CYAN)
    api2 = run_cmd_system('curl -s http://127.0.0.1:8080/api/stats')
    try:
        data2 = json.loads(api2)
        log(f"  Upper usage: {data2['premium_links']['upper']['usage_mbps']:.2f} Mbps", GREEN)
        log(f"  Video active: {data2['premium_links']['upper']['video_active']}", GREEN)
        log(f"  Dynamic active: {data2['premium_links']['upper']['dynamic_active']}", GREEN)
        log(f"  Preemption count: {data2.get('preemption_count', 0)}", GREEN)
        save('test3_api_during_video.json', json.dumps(data2, indent=2))
    except Exception as e:
        log(f"  API errore: {e}", RED)
    output_all.append(f"\n=== API STATS (durante video) ===\n{api2}\n")

    # Attesa fine video
    log("Attesa fine video e ripristino (20s)...", YELLOW)
    time.sleep(20)

    # --- Test 3.8: Flow table dopo ripristino ---
    log("Test 3.8: Flow table S2 dopo ripristino", CYAN)
    flows_restored = get_flows('s2')
    output_all.append(f"\n=== FLOWS S2 DOPO RIPRISTINO ===\n{flows_restored}\n")
    save('test3_flows_s2_restored.txt', flows_restored)

    # --- Test 3.9: API dopo ripristino ---
    log("Test 3.9: API stats dopo ripristino", CYAN)
    api3 = run_cmd_system('curl -s http://127.0.0.1:8080/api/stats')
    try:
        data3 = json.loads(api3)
        log(f"  Video active: {data3['premium_links']['upper']['video_active']}", GREEN)
        log(f"  Dynamic active: {data3['premium_links']['upper']['dynamic_active']}", GREEN)
        log(f"  Preemption totali: {data3.get('preemption_count', 0)}", GREEN)
        log(f"  Eventi: {len(data3.get('events', []))}", GREEN)
        save('test3_api_restored.json', json.dumps(data3, indent=2))
    except Exception as e:
        log(f"  API errore: {e}", RED)
    output_all.append(f"\n=== API STATS (dopo ripristino) ===\n{api3}\n")

    # Kill iperf
    h3.cmd('kill %iperf 2>/dev/null')
    h1.cmd('kill %iperf 2>/dev/null')

    full_output = '\n'.join(output_all)
    save('test3_dynamic_slicing.txt', full_output)

    # Analisi log controller
    log_path = os.path.join(RESULTS_DIR, 'log_dynamic_slicing_controller.txt')
    if os.path.exists(log_path):
        with open(log_path) as f:
            ctrl_log = f.read()
        save('test3_controller_log.txt', ctrl_log)
        log(f"Log controller: {len(ctrl_log.splitlines())} righe", GREEN)
        has_preempt = 'PREEMPTION' in ctrl_log.upper()
        has_alloc = 'ALLOCAZIONE' in ctrl_log.upper()
        has_video = 'video' in ctrl_log.lower() and 'rilevato' in ctrl_log.lower()
        log(f"  Preemption nel log: {'SI' if has_preempt else 'NO'}",
            GREEN if has_preempt else RED)
        log(f"  Allocazione nel log: {'SI' if has_alloc else 'NO'}",
            GREEN if has_alloc else RED)
        log(f"  Video rilevato nel log: {'SI' if has_video else 'NO'}",
            GREEN if has_video else RED)

    stop_all(ctrl, net)
    log("Test 3 COMPLETATO", GREEN)
    return {'status': 'OK', 'pingall_drop': pingall_result}


# =====================================================================
#  TEST 5: D-ITG (GENERAZIONE AVANZATA DI TRAFFICO)
# =====================================================================
def test_ditg():
    log("=" * 65, BOLD)
    log("  TEST 5: D-ITG (GENERAZIONE AVANZATA DI TRAFFICO)", BOLD)
    log("=" * 65, BOLD)
    output_all = []

    # Verifica prerequisiti D-ITG
    for tool in ['ITGSend', 'ITGRecv', 'ITGDec']:
        result = subprocess.run(['which', tool], capture_output=True, text=True)
        if result.returncode != 0:
            log(f"ERRORE: {tool} non trovato. Installa con: sudo apt install d-itg", RED)
            return {'status': 'ERRORE', 'motivo': f'{tool} non trovato'}
        log(f"  {tool}: {result.stdout.strip()}", GREEN)

    # Directory per log D-ITG
    ditg_dir = os.path.join(RESULTS_DIR, 'ditg')
    os.makedirs(ditg_dir, exist_ok=True)

    cleanup()
    ctrl = start_controller('dynamic_slicing_controller.py')
    if not ctrl:
        return {'status': 'ERRORE'}

    net = start_net()

    h1 = net.get('H1')
    h3 = net.get('H3')

    # --- 5.1: Pingall ---
    log("Test 5.1: pingall (atteso: 0% drop)", CYAN)
    pingall_result = net.pingAll(timeout=5)
    output_all.append(f"=== PINGALL ===\nDrop rate: {pingall_result}%\n")
    log(f"  Risultato pingall: {pingall_result}% drop", GREEN if pingall_result == 0 else RED)

    # --- 5.2: Avvio ITGRecv su H3 ---
    log("Test 5.2: Avvio ITGRecv su H3 (signaling port 9000)", CYAN)
    h3.cmd(f'ITGRecv -l {ditg_dir}/recv.log &')
    output_all.append("=== ITGRecv avviato su H3 ===\n")
    time.sleep(3)

    # --- 5.3: Avvio Flow Normale in background ---
    log("Test 5.3: Avvio Flow Normale UDP porta 800 (500 pkt/s, 1000B, 60s)", CYAN)
    normal_send_log = os.path.join(ditg_dir, 'normal_send.log')
    normal_recv_log = os.path.join(ditg_dir, 'normal_recv.log')
    normal_cmd = (f'ITGSend -a 10.0.0.3 -rp 800 -C 500 -c 1000 '
                  f'-t 60000 -T UDP -l {normal_send_log} -x {normal_recv_log} &')
    h1.cmd(normal_cmd)
    output_all.append(f"=== Flow Normale avviato ===\nComando: {normal_cmd}\n")
    log("  Flow Normale avviato. Attesa allocazione (15s)...", YELLOW)
    time.sleep(15)

    # --- 5.4: Verifica flow table S2 ---
    log("Test 5.4: Verifica flow table S2 (attesa priority=110)", CYAN)
    flows_s2 = get_flows('s2')
    has_prio90 = 'priority=110' in flows_s2
    output_all.append(f"=== FLOWS S2 (dopo allocazione flow normale) ===\n{flows_s2}\n")
    output_all.append(f"priority=110 presente: {'SI' if has_prio90 else 'NO'}\n")
    log(f"  priority=110 presente: {'SI' if has_prio90 else 'NO'}",
        GREEN if has_prio90 else RED)

    # --- 5.5: Avvio Flow Video in foreground ---
    log("Test 5.5: Avvio Flow Video UDP porta 9999 (4000 pkt/s, 1400B, 30s)", CYAN)
    video_send_log = os.path.join(ditg_dir, 'video_send.log')
    video_recv_log = os.path.join(ditg_dir, 'video_recv.log')
    video_cmd = (f'ITGSend -a 10.0.0.3 -rp 9999 -C 4000 -c 1400 '
                 f'-t 30000 -T UDP -l {video_send_log} -x {video_recv_log}')
    output_all.append(f"=== Flow Video avviato ===\nComando: {video_cmd}\n")
    log("  Flow Video in foreground (30s bloccanti)...", YELLOW)
    video_output = h1.cmd(video_cmd, timeout=45)
    output_all.append(f"=== Output ITGSend Video ===\n{video_output}\n")
    log(f"  ITGSend video completato", GREEN)

    # --- 5.6: Attesa fine flow normale ---
    log("Test 5.6: Attesa fine flow normale (18s)...", YELLOW)
    time.sleep(18)

    # --- 5.7: Verifica API preemption ---
    log("Test 5.7: Verifica API /api/stats per preemption", CYAN)
    api_out = run_cmd_system('curl -s http://127.0.0.1:8080/api/stats')
    output_all.append(f"=== API /api/stats ===\n{api_out}\n")
    try:
        data = json.loads(api_out)
        preemption_count = data.get('preemption_count', 0)
        log(f"  preemption_count: {preemption_count}",
            GREEN if preemption_count > 0 else RED)
        output_all.append(f"preemption_count: {preemption_count}\n")
    except Exception as e:
        log(f"  API errore: {e}", RED)

    # --- 5.8: Kill ITGRecv ---
    log("Test 5.8: Kill ITGRecv e analisi risultati con ITGDec", CYAN)
    h3.cmd('killall ITGRecv 2>/dev/null')
    time.sleep(2)

    # --- 5.9: Analisi con ITGDec ---
    # Nota: -x (receiver log via signaling) non funziona bene in Mininet,
    # quindi analizziamo il recv.log principale (contiene tutti i flussi)
    # e i send log individuali.
    dec_results = {}
    recv_main_log = os.path.join(ditg_dir, 'recv.log')

    # Analisi recv.log (contiene tutti i flussi con delay/jitter reali)
    log(f"  Analisi recv.log (tutti i flussi): ITGDec {recv_main_log}", CYAN)
    if os.path.exists(recv_main_log):
        try:
            dec = subprocess.run(
                ['ITGDec', recv_main_log],
                capture_output=True, text=True, timeout=30
            )
            # Estrai solo le righe di riepilogo (non i dati per-pacchetto)
            summary_lines = []
            capture = False
            for line in (dec.stdout + dec.stderr).splitlines():
                if 'Flow number' in line or 'TOTAL RESULTS' in line:
                    capture = True
                if capture:
                    summary_lines.append(line)
            dec_summary = '\n'.join(summary_lines)
            dec_results['recv_all'] = dec_summary
            output_all.append(f"=== ITGDec recv.log (tutti i flussi) ===\n{dec_summary}\n")
            log(f"  recv.log:\n{dec_summary}", GREEN)
        except Exception as e:
            output_all.append(f"=== ITGDec recv.log === ERRORE: {e}\n")
            log(f"  ITGDec recv.log errore: {e}", RED)
    else:
        output_all.append(f"=== ITGDec recv.log === File non trovato\n")
        log(f"  File non trovato: {recv_main_log}", RED)

    # Analisi send log individuali (solo statistiche lato mittente)
    for label, send_log in [('video_send', video_send_log), ('normal_send', normal_send_log)]:
        log(f"  Analisi {label}: ITGDec {send_log}", CYAN)
        if os.path.exists(send_log):
            try:
                dec = subprocess.run(
                    ['ITGDec', send_log],
                    capture_output=True, text=True, timeout=30
                )
                summary_lines = []
                capture = False
                for line in (dec.stdout + dec.stderr).splitlines():
                    if 'TOTAL RESULTS' in line:
                        capture = True
                    if capture:
                        summary_lines.append(line)
                dec_summary = '\n'.join(summary_lines)
                dec_results[label] = dec_summary
                output_all.append(f"=== ITGDec {label} ===\n{dec_summary}\n")
                log(f"  {label}:\n{dec_summary}", GREEN)
            except Exception as e:
                output_all.append(f"=== ITGDec {label} === ERRORE: {e}\n")
                log(f"  ITGDec {label} errore: {e}", RED)
        else:
            output_all.append(f"=== ITGDec {label} === File non trovato: {send_log}\n")
            log(f"  File non trovato: {send_log}", RED)

    # Riepilogo
    output_all.append("\n" + "=" * 60)
    output_all.append("RIEPILOGO D-ITG")
    output_all.append("=" * 60)
    output_all.append("Flow Normale: UDP porta 800, 500 pkt/s, 1000B, 60s (~4 Mbps offerti)")
    output_all.append("Flow Video:   UDP porta 9999, 4000 pkt/s, 1400B, 30s (~44.8 Mbps offerti)")
    output_all.append("Link premium: 6 Mbps -> packet loss atteso elevato per il video")

    full_output = '\n'.join(output_all)
    save('test5_ditg.txt', full_output)
    log(f"Risultati salvati in test_results/test5_ditg.txt", GREEN)
    log(f"Log D-ITG salvati in test_results/ditg/", GREEN)

    stop_all(ctrl, net)
    log("Test 5 COMPLETATO", GREEN)
    return {'status': 'OK', 'dec_results': dec_results}


# =====================================================================
#  MAIN
# =====================================================================
def main():
    setLogLevel('warning')

    log("=" * 65, BOLD)
    log("  SUITE DI TEST COMPLETA - SDN Network Slicing", BOLD)
    log(f"  Risultati: {RESULTS_DIR}", BOLD)
    log("=" * 65, BOLD)

    tests = sys.argv[1:] if len(sys.argv) > 1 else ['topology', 'service', 'stress', 'dynamic', 'ditg']
    results = {}

    if 'topology' in tests:
        results['topology'] = test_topology_slicing()
    if 'service' in tests:
        results['service'] = test_service_slicing()
    if 'stress' in tests:
        results['stress'] = test_stress()
    if 'dynamic' in tests:
        results['dynamic'] = test_dynamic_slicing()
    if 'ditg' in tests:
        results['ditg'] = test_ditg()

    # Riepilogo
    save('summary.json', json.dumps(results, indent=2, default=str))

    log("=" * 65, BOLD)
    log("  RIEPILOGO FINALE", BOLD)
    log("=" * 65, BOLD)
    for name, res in results.items():
        status = res.get('status', '?')
        color = GREEN if status == 'OK' else RED
        extra = f" (pingall drop: {res.get('pingall_drop', '?')}%)" if 'pingall_drop' in res else ''
        log(f"  {name}: {status}{extra}", color)

    log(f"\nTutti i risultati in: {RESULTS_DIR}/", GREEN)
    cleanup()

if __name__ == '__main__':
    main()
