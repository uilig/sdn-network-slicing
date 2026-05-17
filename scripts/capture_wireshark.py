#!/usr/bin/env python3
"""
Catture tcpdump per documentazione Wireshark.
Esegue 2 scenari:
  1) Service Slicing: cattura su s2-eth3 (premium) e s2-eth2 (standard)
     con traffico video UDP 9999 + TCP 5001 simultanei
  2) Dynamic Slicing: cattura su s2-eth3 (premium) con preemption
     (UDP 800 poi video UDP 9999)

Salva i .pcap in test_results/pcap/
"""

import sys
import os
import time
import subprocess
import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from topology import PremiumLinkTopology

PCAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'documentazione')
RYU_MANAGER = 'ryu-manager'

def wait_controller(port=6653, timeout=10):
    """Attende che il controller Ryu sia in ascolto."""
    import socket
    for _ in range(timeout * 2):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(('127.0.0.1', port))
            s.close()
            return True
        except:
            time.sleep(0.5)
    return False


def capture_service_slicing():
    """Cattura 1: Service Slicing - premium vs standard."""
    print("\n" + "=" * 60)
    print("CATTURA 1: SERVICE SLICING")
    print("=" * 60)

    # Avvia controller come utente normale (non serve sudo per Ryu)
    ctrl = subprocess.Popen(
        [RYU_MANAGER, 'service_slicing_controller.py'],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print("Controller service_slicing avviato (PID %d)" % ctrl.pid)

    if not wait_controller():
        print("ERRORE: controller non raggiungibile")
        stderr = ctrl.stderr.read().decode()[:500]
        print("STDERR: %s" % stderr)
        ctrl.terminate()
        return

    # Avvia topologia
    topo = PremiumLinkTopology()
    net = Mininet(topo=topo, switch=OVSKernelSwitch,
                  controller=RemoteController('c0', ip='127.0.0.1', port=6653),
                  link=TCLink, autoSetMacs=False)
    net.start()
    time.sleep(3)

    h1, h3 = net.get('H1'), net.get('H3')

    # Pingall per popolare le flow table
    print("Pingall...")
    net.pingAll()
    time.sleep(2)

    # Avvia catture tcpdump
    pcap_premium = os.path.join(PCAP_DIR, 'cattura_premium.pcap')
    pcap_standard = os.path.join(PCAP_DIR, 'cattura_standard.pcap')

    tcpdump_prem = subprocess.Popen(
        ['tcpdump', '-i', 's2-eth3', '-w', pcap_premium, '-c', '500'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    tcpdump_std = subprocess.Popen(
        ['tcpdump', '-i', 's2-eth2', '-w', pcap_standard, '-c', '500'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print("tcpdump avviato su s2-eth3 (premium) e s2-eth2 (standard)")
    time.sleep(1)

    # Avvia server iperf su H3
    h3.cmd('iperf -s -u -p 9999 &')
    h3.cmd('iperf -s -p 5001 &')
    time.sleep(1)

    # Traffico video UDP 9999 (premium link)
    print("Avvio traffico video UDP 9999 (10s)...")
    h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 4M -t 10 &')

    # Traffico TCP 5001 (standard link)
    print("Avvio traffico TCP 5001 (10s)...")
    h1.cmd('iperf -c 10.0.0.3 -p 5001 -t 10 &')

    # Ping per buona misura
    h1.cmd('ping -c 5 10.0.0.3 &')

    print("Attesa 12 secondi per completare il traffico...")
    time.sleep(12)

    # Stop catture
    tcpdump_prem.terminate()
    tcpdump_std.terminate()
    time.sleep(1)

    # Analisi con tshark
    print("\n--- Analisi Premium Link (s2-eth3) ---")
    out = subprocess.run(
        ['tshark', '-r', pcap_premium, '-q', '-z', 'io,stat,1'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("Protocolli su s2-eth3 (premium):")
    out = subprocess.run(
        ['tshark', '-r', pcap_premium, '-q', '-z', 'conv,udp'],
        capture_output=True, text=True
    )
    print(out.stdout[:2000])

    print("\n--- Analisi Standard Link (s2-eth2) ---")
    out = subprocess.run(
        ['tshark', '-r', pcap_standard, '-q', '-z', 'io,stat,1'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("Protocolli su s2-eth2 (standard):")
    out = subprocess.run(
        ['tshark', '-r', pcap_standard, '-q', '-z', 'conv,tcp'],
        capture_output=True, text=True
    )
    print(out.stdout[:2000])

    # Primi pacchetti per verifica
    print("\nPrimi 10 pacchetti su Premium Link (s2-eth3):")
    out = subprocess.run(
        ['tshark', '-r', pcap_premium, '-c', '10',
         '-T', 'fields', '-e', 'frame.number', '-e', 'ip.src',
         '-e', 'ip.dst', '-e', 'ip.proto', '-e', 'udp.dstport',
         '-e', 'tcp.dstport', '-E', 'header=y', '-E', 'separator=\t'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("\nPrimi 10 pacchetti su Standard Link (s2-eth2):")
    out = subprocess.run(
        ['tshark', '-r', pcap_standard, '-c', '10',
         '-T', 'fields', '-e', 'frame.number', '-e', 'ip.src',
         '-e', 'ip.dst', '-e', 'ip.proto', '-e', 'udp.dstport',
         '-e', 'tcp.dstport', '-E', 'header=y', '-E', 'separator=\t'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("\nPcap salvati:")
    print("  %s" % pcap_premium)
    print("  %s" % pcap_standard)

    # Cleanup
    h3.cmd('kill %iperf 2>/dev/null')
    net.stop()
    ctrl.terminate()
    ctrl.wait()
    time.sleep(2)


def capture_dynamic_slicing():
    """Cattura 2: Dynamic Slicing - preemption visibile."""
    print("\n" + "=" * 60)
    print("CATTURA 2: DYNAMIC SLICING (PREEMPTION)")
    print("=" * 60)

    # Avvia controller come utente normale (non serve sudo per Ryu)
    ctrl = subprocess.Popen(
        [RYU_MANAGER, 'dynamic_slicing_controller.py'],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print("Controller dynamic_slicing avviato (PID %d)" % ctrl.pid)

    if not wait_controller():
        print("ERRORE: controller non raggiungibile")
        stderr = ctrl.stderr.read().decode()[:500]
        print("STDERR: %s" % stderr)
        ctrl.terminate()
        return

    # Avvia topologia
    topo = PremiumLinkTopology()
    net = Mininet(topo=topo, switch=OVSKernelSwitch,
                  controller=RemoteController('c0', ip='127.0.0.1', port=6653),
                  link=TCLink, autoSetMacs=False)
    net.start()
    time.sleep(3)

    h1, h3 = net.get('H1'), net.get('H3')

    # Pingall
    print("Pingall...")
    net.pingAll()
    time.sleep(2)

    # Cattura sul premium link per tutta la durata del test
    pcap_preemption = os.path.join(PCAP_DIR, 'cattura_dynamic.pcap')
    tcpdump_proc = subprocess.Popen(
        ['tcpdump', '-i', 's2-eth3', '-w', pcap_preemption],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    print("tcpdump avviato su s2-eth3 (premium)")
    time.sleep(1)

    # Server su H3
    h3.cmd('iperf -s -u -p 800 &')
    h3.cmd('iperf -s -u -p 9999 &')
    time.sleep(1)

    # Fase 1: traffico dinamico UDP 800 (viene allocato su premium dopo ~10s)
    print("Fase 1: avvio traffico UDP 800 (1 Mbps, 50s)...")
    h1.cmd('iperf -c 10.0.0.3 -u -p 800 -b 1M -t 50 &')

    print("Attesa 15s per allocazione dinamica...")
    time.sleep(15)

    # Verifica regola prio 90
    flows = subprocess.run(
        ['ovs-ofctl', 'dump-flows', 's2', '--protocols=OpenFlow13'],
        capture_output=True, text=True
    )
    if 'priority=110' in flows.stdout:
        print("OK: regola priority=110 presente (traffico UDP 800 su premium)")
    else:
        print("ATTENZIONE: regola priority=110 non trovata")

    # Fase 2: avvio video -> preemption
    print("Fase 2: avvio traffico video UDP 9999 (4 Mbps, 20s)...")
    h1.cmd('iperf -c 10.0.0.3 -u -p 9999 -b 4M -t 20 &')

    print("Attesa 25s per preemption e fine video...")
    time.sleep(25)

    # Stop cattura
    tcpdump_proc.terminate()
    time.sleep(1)

    # Analisi con tshark
    print("\n--- Analisi Preemption (s2-eth3) ---")
    out = subprocess.run(
        ['tshark', '-r', pcap_preemption, '-q', '-z', 'io,stat,5'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("Conversazioni UDP su premium link:")
    out = subprocess.run(
        ['tshark', '-r', pcap_preemption, '-q', '-z', 'conv,udp'],
        capture_output=True, text=True
    )
    print(out.stdout[:2000])

    # I/O stat per secondo per vedere la transizione
    print("\nI/O stat per secondo (per osservare la preemption):")
    out = subprocess.run(
        ['tshark', '-r', pcap_preemption, '-q', '-z', 'io,stat,1,udp.port==800,udp.port==9999'],
        capture_output=True, text=True
    )
    print(out.stdout)

    print("\nPcap salvato:")
    print("  %s" % pcap_preemption)

    # Cleanup
    h3.cmd('kill %iperf 2>/dev/null')
    net.stop()
    ctrl.terminate()
    ctrl.wait()
    time.sleep(2)


if __name__ == '__main__':
    setLogLevel('warning')
    os.makedirs(PCAP_DIR, exist_ok=True)

    print("Catture Wireshark per documentazione")
    print("Pcap salvati in: %s" % PCAP_DIR)

    capture_service_slicing()
    capture_dynamic_slicing()

    print("\n" + "=" * 60)
    print("CATTURE COMPLETATE")
    print("=" * 60)
    print("\nFile .pcap generati:")
    for f in sorted(os.listdir(PCAP_DIR)):
        if f.endswith('.pcap'):
            path = os.path.join(PCAP_DIR, f)
            size = os.path.getsize(path)
            print("  %s (%d bytes)" % (f, size))
    print("\nPer aprirli in Wireshark:")
    print("  wireshark documentazione/cattura_premium.pcap")
    print("  wireshark documentazione/cattura_standard.pcap")
    print("  wireshark documentazione/cattura_dynamic.pcap")
