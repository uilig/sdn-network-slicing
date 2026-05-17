#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
D-ITG TEST SCRIPT - SDN NETWORK SLICING
================================================================================

Script per eseguire test avanzati di traffico utilizzando D-ITG
(Distributed Internet Traffic Generator).

D-ITG e uno strumento professionale per la generazione e misurazione del
traffico di rete. Rispetto a iperf, offre:
- Generazione di traffico con distribuzione statistica realistica
- Supporto per molteplici protocolli (UDP, TCP, ICMP, etc.)
- Misurazione precisa di latenza, jitter e packet loss
- Capacita di emulare diversi tipi di traffico (VoIP, video, DNS, etc.)

PREREQUISITI
------------
D-ITG deve essere installato sul sistema. Su Ubuntu/Debian:
    sudo apt install d-itg

UTILIZZO
--------
Questo script genera i comandi D-ITG per testare la topologia del SDN Network Slicing.
I test includono:
1. Traffico video simulato su Premium Link (CBR ad alta banda)
2. Traffico VoIP simulato (pacchetti piccoli, timing critico)
3. Traffico best-effort su percorso standard

================================================================================
"""

import os
import sys
import subprocess
import argparse
from datetime import datetime


# =============================================================================
# CONFIGURAZIONE
# =============================================================================

RESULTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'test_results', 'ditg')

HOSTS = {
    'H1': '10.0.0.1',
    'H2': '10.0.0.2',
    'H3': '10.0.0.3',
    'H4': '10.0.0.4',
}

VIDEO_PORT = 9999       # Porta per traffico video (Premium Link)
VOIP_PORT = 5060        # Porta SIP standard
NORMAL_PORT = 5001      # Porta per traffico normale (percorso standard)
DYNAMIC_PORT = 800      # Porta per traffico dinamico

DEFAULT_DURATION = 30


# =============================================================================
# CLASSE PRINCIPALE
# =============================================================================

class DITGTester:
    """
    Classe per l'esecuzione di test D-ITG nella topologia a 6 switch
    con Premium Links.
    """

    def __init__(self, duration=DEFAULT_DURATION):
        self.duration = duration
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.results_path = os.path.join(RESULTS_DIR, self.timestamp)
        os.makedirs(self.results_path, exist_ok=True)

        print("=" * 70)
        print("D-ITG TEST SUITE - SDN Network Slicing (Premium Links)")
        print("=" * 70)
        print(f"Durata test: {self.duration} secondi")
        print(f"Directory risultati: {self.results_path}")
        print("=" * 70)

    def check_prerequisites(self):
        """Verifica che D-ITG sia installato."""
        print("\n[1/5] Verifica prerequisiti...")

        for tool in ['ITGSend', 'ITGRecv']:
            try:
                result = subprocess.run(['which', tool],
                                       capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"  ERRORE: {tool} non trovato!")
                    print("  Installa D-ITG con: sudo apt install d-itg")
                    return False
                print(f"  {tool}: {result.stdout.strip()}")
            except Exception as e:
                print(f"  ERRORE: {e}")
                return False

        # Verifica opzionale ITGDec
        try:
            result = subprocess.run(['which', 'ITGDec'],
                                   capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  ITGDec: {result.stdout.strip()}")
            else:
                print("  ATTENZIONE: ITGDec non trovato")
        except:
            pass

        print("  Prerequisiti OK!")
        return True

    def print_mininet_commands(self):
        """Stampa i comandi D-ITG da eseguire in Mininet."""
        print("\n" + "=" * 70)
        print("COMANDI D-ITG PER MININET")
        print("=" * 70)
        print("\nCopiare i seguenti comandi nella CLI di Mininet:\n")

        # Test 1: Video su Premium Link
        print("--- TEST 1: Traffico Video (Premium Link) ---")
        print(f"# Sul ricevitore (H3):")
        print(f"H3 ITGRecv -l {self.results_path}/video_recv.log &")
        print(f"\n# Sul mittente (H1), dopo qualche secondo:")
        print(f"H1 ITGSend -a {HOSTS['H3']} -rp {VIDEO_PORT} -C 900 -c 1400 "
              f"-t {self.duration * 1000} -T UDP -l {self.results_path}/video_send.log")
        print()

        # Test 2: VoIP
        print("--- TEST 2: Traffico VoIP ---")
        print(f"# Sul ricevitore (H3):")
        print(f"H3 ITGRecv -l {self.results_path}/voip_recv.log &")
        print(f"\n# Sul mittente (H1):")
        print(f"H1 ITGSend -a {HOSTS['H3']} -rp {VOIP_PORT} -C 50 -c 172 "
              f"-t {self.duration * 1000} -T UDP -l {self.results_path}/voip_send.log")
        print()

        # Test 3: Best-effort su percorso standard
        print("--- TEST 3: Traffico Best-Effort (Percorso Standard) ---")
        print(f"# Sul ricevitore (H3):")
        print(f"H3 ITGRecv -l {self.results_path}/besteffort_recv.log &")
        print(f"\n# Sul mittente (H1):")
        print(f"H1 ITGSend -a {HOSTS['H3']} -rp {NORMAL_PORT} -C 500 -c 1000 "
              f"-t {self.duration * 1000} -T UDP -l {self.results_path}/besteffort_send.log")
        print()

        # Test 4: Traffico dinamico (per testare preemption)
        print("--- TEST 4: Traffico Dinamico (per Preemption) ---")
        print(f"# Sul ricevitore (H3):")
        print(f"H3 ITGRecv -l {self.results_path}/dynamic_recv.log &")
        print(f"\n# Sul mittente (H1) - avviare prima del video:")
        print(f"H1 ITGSend -a {HOSTS['H3']} -rp {DYNAMIC_PORT} -C 200 -c 500 "
              f"-t {self.duration * 2 * 1000} -T UDP -l {self.results_path}/dynamic_send.log &")
        print(f"\n# Poi avviare il video dopo 15 secondi per triggerre preemption:")
        print(f"H1 ITGSend -a {HOSTS['H3']} -rp {VIDEO_PORT} -C 600 -c 1400 "
              f"-t {self.duration * 1000} -T UDP")
        print()

        # Analisi
        print("--- ANALISI RISULTATI (dopo i test) ---")
        for log_name in ['video_recv', 'voip_recv', 'besteffort_recv', 'dynamic_recv']:
            print(f"ITGDec {self.results_path}/{log_name}.log")
        print()

        print("=" * 70)
        print("NOTA: Assicurarsi che il controller SDN sia in esecuzione")
        print("      prima di avviare i test.")
        print("=" * 70)

    def analyze_results(self):
        """Analizza i risultati dei test."""
        print("\n[5/5] Analisi risultati...")

        log_files = [
            ('video_recv.log', 'Traffico Video (Premium Link)'),
            ('voip_recv.log', 'Traffico VoIP'),
            ('besteffort_recv.log', 'Traffico Best-Effort (Standard)'),
            ('dynamic_recv.log', 'Traffico Dinamico'),
        ]

        report_path = os.path.join(self.results_path, 'report.txt')

        with open(report_path, 'w') as report:
            report.write("=" * 70 + "\n")
            report.write("REPORT TEST D-ITG - SDN Network Slicing\n")
            report.write(f"Data: {self.timestamp}\n")
            report.write("=" * 70 + "\n\n")

            for log_file, test_name in log_files:
                log_path = os.path.join(self.results_path, log_file)

                if os.path.exists(log_path):
                    report.write(f"--- {test_name} ---\n")
                    try:
                        result = subprocess.run(
                            ['ITGDec', log_path],
                            capture_output=True, text=True, timeout=30
                        )
                        if result.returncode == 0:
                            report.write(result.stdout)
                        else:
                            report.write(f"Errore: {result.stderr}\n")
                    except FileNotFoundError:
                        report.write("ITGDec non disponibile\n")
                    except subprocess.TimeoutExpired:
                        report.write("Timeout nell'analisi\n")
                    report.write("\n")
                else:
                    report.write(f"--- {test_name} ---\n")
                    report.write(f"File non trovato: {log_file}\n\n")

        print(f"  Report salvato in: {report_path}")

    def run(self):
        """Esegue la suite completa di test D-ITG."""
        if not self.check_prerequisites():
            print("\nD-ITG non trovato. Verranno generati solo i comandi.\n")

        print("\n[2/5] Generazione comandi di test...")
        self.print_mininet_commands()

        print("\n" + "=" * 70)
        print("TEST D-ITG PRONTI")
        print("=" * 70)
        print(f"\nEseguire i comandi nella CLI di Mininet.")
        print(f"I risultati verranno salvati in: {self.results_path}")
        print(f"\nPer analizzare i risultati dopo i test:")
        print(f"  python3 {__file__} --analyze {self.results_path}")
        print()


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='D-ITG Test Suite - SDN Network Slicing (Premium Links)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python3 ditg_tests.py                    # Genera comandi
  python3 ditg_tests.py -d 60             # Test di 60 secondi
  python3 ditg_tests.py --analyze PATH    # Analizza risultati
        """
    )

    parser.add_argument('-d', '--duration', type=int, default=DEFAULT_DURATION,
                        help=f'Durata test in secondi (default: {DEFAULT_DURATION})')
    parser.add_argument('--analyze', type=str, metavar='PATH',
                        help='Analizza risultati nella directory specificata')

    args = parser.parse_args()

    if args.analyze:
        if os.path.isdir(args.analyze):
            tester = DITGTester()
            tester.results_path = args.analyze
            tester.analyze_results()
        else:
            print(f"Directory non trovata: {args.analyze}")
            sys.exit(1)
    else:
        tester = DITGTester(duration=args.duration)
        tester.run()


if __name__ == '__main__':
    main()
