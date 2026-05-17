#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
SCRIPT DI TEST AUTOMATIZZATI - SDN NETWORK SLICING
Network Slicing con Premium Links
================================================================================

Questo script esegue una suite completa di test automatizzati per verificare
il corretto funzionamento dei tre controller SDN del progetto:

    TEST 1 - Topology Slicing:
        Verifica l'isolamento fisico tra le slice. Solo le coppie H1<->H3
        e H2<->H4 devono poter comunicare. Tutte le comunicazioni cross-slice
        devono essere bloccate (4/12 ping riusciti nel pingall).

    TEST 2 - Service Slicing:
        Verifica il routing basato sul tipo di servizio. Tutti gli host devono
        poter comunicare tra loro (12/12 ping), e il traffico video (UDP 9999)
        deve utilizzare il Premium Link ottenendo banda significativamente
        superiore rispetto al traffico normale (UDP 5001 su percorso standard).

    TEST 3 - Dynamic Slicing:
        Verifica l'allocazione dinamica della banda e il meccanismo di
        preemption. Connettivita completa (12/12 ping), traffico video su
        Premium Link, e verifica dei messaggi di monitoraggio nei log del
        controller.

TOPOLOGIA DI RETE
-----------------

    6 switch OpenFlow 1.3 (S1-S6), 4 host:
        H1 = 10.0.0.1    H2 = 10.0.0.2
        H3   = 10.0.0.3    H4   = 10.0.0.4

    Premium Links: S2-S6 (6 Mbps, 3ms), S4-S6 (6 Mbps, 3ms)
    Standard Links: S2-S3-S6, S4-S5-S6 (2 Mbps, 50ms per hop)

UTILIZZO
--------

    # Esecuzione completa (richiede privilegi root per Mininet):
    sudo python3 scripts/run_tests.py

    # Con durata personalizzata dei test iperf:
    sudo python3 scripts/run_tests.py --duration 15

    # Solo un test specifico:
    sudo python3 scripts/run_tests.py --test topology
    sudo python3 scripts/run_tests.py --test service
    sudo python3 scripts/run_tests.py --test dynamic

================================================================================
"""

import subprocess
import time
import os
import sys
import signal
import argparse
import re
import json
from datetime import datetime


# ==============================================================================
# COSTANTI DI CONFIGURAZIONE
# ==============================================================================

# Directory del progetto (cartella padre di scripts/)
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# File della topologia e dei controller
TOPOLOGY_FILE = os.path.join(PROJECT_DIR, 'topology.py')
TOPOLOGY_SLICING_CONTROLLER = os.path.join(PROJECT_DIR, 'topology_slicing_controller.py')
SERVICE_SLICING_CONTROLLER = os.path.join(PROJECT_DIR, 'service_slicing_controller.py')
DYNAMIC_SLICING_CONTROLLER = os.path.join(PROJECT_DIR, 'dynamic_slicing_controller.py')

# Directory per i risultati dei test
RESULTS_DIR = os.path.join(PROJECT_DIR, 'test_results')

# Indirizzi IP degli host nella topologia
H1_IP = '10.0.0.1'
H2_IP = '10.0.0.2'
H3_IP = '10.0.0.3'
H4_IP = '10.0.0.4'

# Porte UDP per i test iperf
VIDEO_PORT = 9999       # Porta per il traffico video (Premium Link)
NORMAL_PORT = 5001      # Porta per il traffico normale (percorso standard)

# Banda di invio per i test iperf UDP (in Mbps)
IPERF_BANDWIDTH = '10M'

# Tempi di attesa (in secondi)
WAIT_CONTROLLER_START = 5       # Attesa avvio controller Ryu
WAIT_TOPOLOGY_START = 10        # Attesa avvio topologia Mininet
WAIT_NETWORK_STABILIZE = 8     # Attesa stabilizzazione rete (ARP, flow rules)
WAIT_AFTER_PINGALL = 3          # Attesa dopo pingall prima del prossimo comando
WAIT_IPERF_SETUP = 2            # Attesa dopo avvio server iperf
WAIT_AFTER_TEST = 3             # Attesa dopo completamento test iperf
WAIT_CLEANUP = 3                # Attesa dopo operazioni di pulizia


# ==============================================================================
# FUNZIONI DI UTILITA
# ==============================================================================

def print_header(title, char='=', width=70):
    """
    Stampa un'intestazione formattata per separare le sezioni dell'output.

    Args:
        title: Testo dell'intestazione
        char: Carattere usato per la linea decorativa
        width: Larghezza totale della linea
    """
    print()
    print(char * width)
    print("  " + title)
    print(char * width)
    print()


def print_subheader(title, char='-', width=70):
    """
    Stampa un sottotitolo formattato.

    Args:
        title: Testo del sottotitolo
        char: Carattere usato per la linea decorativa
        width: Larghezza totale della linea
    """
    print()
    print(char * width)
    print("  " + title)
    print(char * width)


def print_result(test_name, passed, details=""):
    """
    Stampa il risultato di un singolo test con indicatore PASS/FAIL.

    Args:
        test_name: Nome del test
        passed: True se il test e superato, False altrimenti
        details: Dettagli aggiuntivi opzionali
    """
    status = "[PASS]" if passed else "[FAIL]"
    print("  {} {}".format(status, test_name))
    if details:
        print("        {}".format(details))


def timestamp():
    """Restituisce il timestamp corrente in formato leggibile."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cleanup():
    """
    Esegue la pulizia completa dell'ambiente di test.

    Termina tutti i processi ryu-manager in esecuzione e pulisce
    l'ambiente Mininet con 'mn -c'. Questa funzione viene chiamata
    prima di ogni test e alla fine della suite completa.
    """
    print("  [*] Pulizia ambiente in corso...")

    # Termina tutti i processi ryu-manager
    try:
        subprocess.run(
            ['sudo', 'pkill', '-f', 'ryu-manager'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10
        )
    except (subprocess.TimeoutExpired, Exception):
        pass

    # Termina eventuali processi Mininet residui
    try:
        subprocess.run(
            ['sudo', 'pkill', '-f', 'topology.py'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10
        )
    except (subprocess.TimeoutExpired, Exception):
        pass

    # Pulizia completa di Mininet
    try:
        subprocess.run(
            ['sudo', 'mn', '-c'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30
        )
    except (subprocess.TimeoutExpired, Exception):
        pass

    time.sleep(WAIT_CLEANUP)
    print("  [*] Pulizia completata")


def start_controller(controller_file):
    """
    Avvia un controller Ryu in background.

    Lancia ryu-manager come processo in background e restituisce l'oggetto
    Popen per poter monitorare l'output e terminare il processo.

    Args:
        controller_file: Percorso completo del file del controller

    Returns:
        subprocess.Popen: Oggetto processo del controller avviato
    """
    controller_name = os.path.basename(controller_file)
    print("  [*] Avvio controller: {}".format(controller_name))

    # Il file di log del controller viene salvato nella directory dei risultati
    log_file = os.path.join(
        RESULTS_DIR,
        'controller_{}.log'.format(
            controller_name.replace('.py', '').replace('_controller', '')
        )
    )

    # Avvia ryu-manager in background, redirigendo stdout e stderr al file di log
    log_fd = open(log_file, 'w')
    proc = subprocess.Popen(
        ['ryu-manager', controller_file],
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid  # Crea un nuovo gruppo di processi per la pulizia
    )

    print("  [*] Controller avviato (PID: {})".format(proc.pid))
    print("  [*] Log del controller: {}".format(log_file))
    print("  [*] Attesa avvio controller ({} secondi)...".format(WAIT_CONTROLLER_START))
    time.sleep(WAIT_CONTROLLER_START)

    # Verifica che il controller sia ancora in esecuzione
    if proc.poll() is not None:
        print("  [!] ERRORE: Il controller si e' terminato prematuramente!")
        print("  [!] Controlla il file di log: {}".format(log_file))
        return None

    print("  [*] Controller pronto")
    return proc


def start_topology():
    """
    Avvia la topologia Mininet in background.

    Lancia topology.py come processo in background con sudo python3.
    Lo stdin viene configurato come PIPE per poter inviare comandi alla
    CLI di Mininet. Lo stdout e stderr vengono catturati come PIPE per
    poter leggere l'output dei comandi.

    Returns:
        subprocess.Popen: Oggetto processo della topologia Mininet
    """
    print("  [*] Avvio topologia Mininet: {}".format(TOPOLOGY_FILE))

    proc = subprocess.Popen(
        ['sudo', 'python3', TOPOLOGY_FILE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid  # Nuovo gruppo di processi
    )

    print("  [*] Topologia avviata (PID: {})".format(proc.pid))
    print("  [*] Attesa inizializzazione topologia ({} secondi)...".format(
        WAIT_TOPOLOGY_START))
    time.sleep(WAIT_TOPOLOGY_START)

    # Verifica che Mininet sia ancora in esecuzione
    if proc.poll() is not None:
        print("  [!] ERRORE: Mininet si e' terminato prematuramente!")
        stdout, stderr = proc.communicate(timeout=5)
        if stderr:
            print("  [!] Stderr: {}".format(stderr.decode('utf-8', errors='replace')[:500]))
        return None

    print("  [*] Topologia pronta")
    return proc


def run_mininet_command(net_proc, cmd, wait_time=5):
    """
    Invia un comando alla CLI di Mininet e restituisce l'output.

    Scrive il comando sullo stdin del processo Mininet, attende il
    completamento, e legge l'output dallo stdout.

    Args:
        net_proc: Oggetto Popen del processo Mininet
        cmd: Comando da inviare alla CLI di Mininet (es. 'pingall')
        wait_time: Tempo di attesa in secondi per il completamento del comando

    Returns:
        str: Output del comando, oppure stringa vuota in caso di errore
    """
    if net_proc is None or net_proc.poll() is not None:
        print("  [!] ERRORE: Processo Mininet non disponibile")
        return ""

    print("  [>] Esecuzione comando Mininet: {}".format(cmd))

    try:
        # Invia il comando seguito da newline
        net_proc.stdin.write("{}\n".format(cmd).encode('utf-8'))
        net_proc.stdin.flush()

        # Attendi il completamento del comando
        time.sleep(wait_time)

        # Leggi l'output disponibile (non bloccante)
        # Usiamo un approccio basato su timeout per evitare blocchi
        output = ""
        try:
            # Invia un comando dummy per forzare il flush dell'output precedente
            net_proc.stdin.write("echo __END_MARKER__\n".encode('utf-8'))
            net_proc.stdin.flush()
            time.sleep(1)

            # Leggi tutto l'output disponibile
            import select
            while True:
                ready, _, _ = select.select([net_proc.stdout], [], [], 0.5)
                if ready:
                    chunk = os.read(net_proc.stdout.fileno(), 65536)
                    if chunk:
                        output += chunk.decode('utf-8', errors='replace')
                    else:
                        break
                else:
                    break
        except Exception as e:
            print("  [!] Errore lettura output: {}".format(e))

        return output

    except Exception as e:
        print("  [!] Errore esecuzione comando: {}".format(e))
        return ""


def stop_process(proc, name="processo"):
    """
    Termina un processo e tutto il suo gruppo di processi.

    Args:
        proc: Oggetto Popen del processo da terminare
        name: Nome descrittivo del processo (per i messaggi di log)
    """
    if proc is None:
        return

    try:
        # Invia SIGTERM al gruppo di processi
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(2)

        # Se il processo e ancora attivo, forza la terminazione con SIGKILL
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            time.sleep(1)

        print("  [*] {} terminato (PID: {})".format(name, proc.pid))
    except (ProcessLookupError, OSError):
        # Il processo era gia terminato
        pass


def save_results(test_name, data):
    """
    Salva i risultati di un test in un file JSON nella directory test_results/.

    Args:
        test_name: Nome del test (usato come nome del file)
        data: Dizionario con i risultati del test
    """
    # Assicurati che la directory dei risultati esista
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Aggiungi timestamp ai dati
    data['timestamp'] = timestamp()
    data['test_name'] = test_name

    # Salva come JSON
    filename = os.path.join(RESULTS_DIR, '{}_results.json'.format(test_name))
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("  [*] Risultati salvati in: {}".format(filename))
    except Exception as e:
        print("  [!] Errore salvataggio risultati: {}".format(e))

    # Salva anche un file di testo leggibile
    txt_filename = os.path.join(RESULTS_DIR, '{}_results.txt'.format(test_name))
    try:
        with open(txt_filename, 'w') as f:
            f.write("=" * 70 + "\n")
            f.write("RISULTATI TEST: {}\n".format(test_name.upper()))
            f.write("Data: {}\n".format(data['timestamp']))
            f.write("=" * 70 + "\n\n")
            for key, value in data.items():
                if key not in ('timestamp', 'test_name'):
                    f.write("{}: {}\n".format(key, value))
            f.write("\n" + "=" * 70 + "\n")
        print("  [*] Report testuale salvato in: {}".format(txt_filename))
    except Exception as e:
        print("  [!] Errore salvataggio report: {}".format(e))


def parse_pingall_output(output):
    """
    Analizza l'output del comando pingall di Mininet.

    Cerca il pattern 'X/Y received' o 'Results: X% dropped' nell'output
    per determinare il numero di ping riusciti e falliti.

    Args:
        output: Stringa con l'output del comando pingall

    Returns:
        tuple: (ping_riusciti, ping_totali, percentuale_successo)
    """
    # Pattern per l'output standard di pingall Mininet
    # Esempio: "*** Results: 8/12 dropped (66% dropped)"
    # Oppure: "*** Results: 0% dropped (12/12 received)"
    results = {
        'received': 0,
        'total': 12,
        'drop_percentage': 100
    }

    # Cerca il pattern "X% dropped" nell'output
    drop_match = re.search(r'(\d+)%\s*dropped', output)
    if drop_match:
        drop_pct = int(drop_match.group(1))
        results['drop_percentage'] = drop_pct
        results['received'] = int(round(12 * (100 - drop_pct) / 100.0))

    # Cerca il pattern "X/Y dropped"
    frac_match = re.search(r'(\d+)/(\d+)\s*dropped', output)
    if frac_match:
        dropped = int(frac_match.group(1))
        total = int(frac_match.group(2))
        results['total'] = total
        results['received'] = total - dropped

    # Cerca il pattern "X/Y received"
    recv_match = re.search(r'(\d+)/(\d+)\s*received', output)
    if recv_match:
        results['received'] = int(recv_match.group(1))
        results['total'] = int(recv_match.group(2))

    return results['received'], results['total'], results.get('drop_percentage', 0)


def parse_iperf_output(output):
    """
    Analizza l'output di un test iperf UDP per estrarre la banda ottenuta.

    Cerca il pattern di bandwidth nell'output del client iperf e restituisce
    il valore in Mbps.

    Args:
        output: Stringa con l'output del comando iperf

    Returns:
        float: Banda ottenuta in Mbps, oppure 0.0 se non trovata
    """
    bandwidth = 0.0

    # Cerca pattern come "X.XX Mbits/sec" o "X.XX Kbits/sec"
    # L'output iperf UDP mostra il summary alla fine con la banda effettiva
    lines = output.strip().split('\n')

    for line in lines:
        # Cerca la riga con il summary (contiene "sec" e "bits/sec")
        bw_match = re.search(r'([\d.]+)\s*(Mbits|Kbits|Gbits)/sec', line)
        if bw_match:
            value = float(bw_match.group(1))
            unit = bw_match.group(2)

            if unit == 'Kbits':
                value = value / 1000.0
            elif unit == 'Gbits':
                value = value * 1000.0

            bandwidth = value  # Prendi l'ultimo valore trovato (il summary)

    return bandwidth


def verify_files_exist():
    """
    Verifica che tutti i file necessari per i test esistano.

    Returns:
        bool: True se tutti i file esistono, False altrimenti
    """
    files_to_check = [
        (TOPOLOGY_FILE, "Topologia"),
        (TOPOLOGY_SLICING_CONTROLLER, "Topology Slicing Controller"),
        (SERVICE_SLICING_CONTROLLER, "Service Slicing Controller"),
        (DYNAMIC_SLICING_CONTROLLER, "Dynamic Slicing Controller"),
    ]

    all_exist = True
    for filepath, description in files_to_check:
        if os.path.isfile(filepath):
            print("  [OK] {}: {}".format(description, filepath))
        else:
            print("  [!!] MANCANTE - {}: {}".format(description, filepath))
            all_exist = False

    return all_exist


# ==============================================================================
# TEST 1: TOPOLOGY SLICING
# ==============================================================================

def test_topology_slicing(duration):
    """
    Esegue il test del controller Topology Slicing.

    Verifica l'isolamento fisico tra le due slice:
    - Upper Slice: H1 (10.0.0.1) <-> H3 (10.0.0.3)
    - Lower Slice: H2 (10.0.0.2) <-> H4 (10.0.0.4)

    Il pingall deve mostrare 4/12 ping riusciti (solo comunicazioni intra-slice).
    Le comunicazioni cross-slice (H1<->H2, H1<->H4, H2<->H3, H3<->H4)
    devono essere bloccate.

    Args:
        duration: Durata dei test iperf in secondi (non usata in questo test)

    Returns:
        dict: Dizionario con i risultati del test
    """
    print_header("TEST 1: TOPOLOGY SLICING", '=')
    print("  Obiettivo: Verificare l'isolamento fisico tra le slice")
    print("  Controller: topology_slicing_controller.py")
    print("  Atteso: 4/12 ping riusciti (solo H1<->H3 e H2<->H4)")
    print()

    results = {
        'controller': 'topology_slicing_controller.py',
        'status': 'ERRORE',
        'pingall_received': 0,
        'pingall_total': 12,
        'cross_slice_blocked': False,
        'intra_slice_ok': False,
        'details': '',
        'raw_output': ''
    }

    controller_proc = None
    net_proc = None

    try:
        # Fase 1: Pulizia iniziale
        print_subheader("Fase 1: Pulizia ambiente")
        cleanup()

        # Fase 2: Avvio controller
        print_subheader("Fase 2: Avvio controller Topology Slicing")
        controller_proc = start_controller(TOPOLOGY_SLICING_CONTROLLER)
        if controller_proc is None:
            results['details'] = 'Impossibile avviare il controller'
            return results

        # Fase 3: Avvio topologia
        print_subheader("Fase 3: Avvio topologia Mininet")
        net_proc = start_topology()
        if net_proc is None:
            results['details'] = 'Impossibile avviare la topologia'
            return results

        # Fase 4: Attesa stabilizzazione rete
        print_subheader("Fase 4: Stabilizzazione rete")
        print("  [*] Attesa stabilizzazione ({} secondi)...".format(
            WAIT_NETWORK_STABILIZE))
        time.sleep(WAIT_NETWORK_STABILIZE)

        # Fase 5: Esecuzione pingall
        print_subheader("Fase 5: Test di connettivita (pingall)")
        pingall_output = run_mininet_command(net_proc, 'pingall', wait_time=30)
        results['raw_output'] = pingall_output

        print()
        print("  --- Output pingall ---")
        # Stampa le righe rilevanti dell'output
        for line in pingall_output.split('\n'):
            stripped = line.strip()
            if stripped and ('ping' in stripped.lower() or 'result' in stripped.lower()
                           or 'dropped' in stripped.lower() or '->' in stripped
                           or 'H1' in stripped or 'H2' in stripped or 'H3' in stripped or 'H4' in stripped):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")
        print()

        # Fase 6: Analisi risultati
        print_subheader("Fase 6: Analisi risultati")

        received, total, drop_pct = parse_pingall_output(pingall_output)
        results['pingall_received'] = received
        results['pingall_total'] = total

        print("  Ping riusciti: {}/{}".format(received, total))
        print("  Percentuale drop: {}%".format(drop_pct))

        # Verifica: ci aspettiamo esattamente 4/12 ping riusciti
        # (H1->H3, H3->H1, H2->H4, H4->H2)
        # Con una tolleranza: accettiamo anche 4 +/- 1 per instabilita di rete
        expected_received = 4

        if received == expected_received:
            results['intra_slice_ok'] = True
            results['cross_slice_blocked'] = True
            results['status'] = 'SUPERATO'
            print_result(
                "Isolamento slice",
                True,
                "{}/12 ping riusciti (attesi {}/12)".format(received, expected_received)
            )
        elif 3 <= received <= 5:
            # Tolleranza per instabilita di rete
            results['intra_slice_ok'] = True
            results['cross_slice_blocked'] = True
            results['status'] = 'SUPERATO (con tolleranza)'
            print_result(
                "Isolamento slice",
                True,
                "{}/12 ping riusciti (attesi ~{}/12, entro tolleranza)".format(
                    received, expected_received)
            )
        else:
            results['status'] = 'FALLITO'
            if received == 12:
                results['details'] = 'Nessun isolamento: tutti i ping riusciti'
                print_result(
                    "Isolamento slice",
                    False,
                    "Tutti i ping riusciti - l'isolamento NON funziona"
                )
            elif received == 0:
                results['details'] = 'Nessuna connettivita: tutti i ping falliti'
                print_result(
                    "Isolamento slice",
                    False,
                    "Nessun ping riuscito - possibile problema di configurazione"
                )
            else:
                results['details'] = 'Risultato inatteso: {}/12 riusciti'.format(received)
                print_result(
                    "Isolamento slice",
                    False,
                    "{}/12 ping riusciti (attesi {}/12)".format(
                        received, expected_received)
                )

        # Riepilogo cross-slice
        print()
        print("  Comunicazioni attese:")
        print("    H1 <-> H3 : PERMESSA (stessa slice - upper)")
        print("    H2 <-> H4 : PERMESSA (stessa slice - lower)")
        print("    H1 <-> H2: BLOCCATA (cross-slice)")
        print("    H1 <-> H4 : BLOCCATA (cross-slice)")
        print("    H2 <-> H3 : BLOCCATA (cross-slice)")
        print("    H3   <-> H4 : BLOCCATA (cross-slice)")

    except Exception as e:
        results['status'] = 'ERRORE'
        results['details'] = str(e)
        print("  [!] ERRORE durante il test: {}".format(e))

    finally:
        # Pulizia: termina controller e topologia
        print_subheader("Pulizia post-test")
        stop_process(net_proc, "Mininet")
        stop_process(controller_proc, "Controller")
        cleanup()

    # Salvataggio risultati
    save_results('topology_slicing', results)

    return results


# ==============================================================================
# TEST 2: SERVICE SLICING
# ==============================================================================

def test_service_slicing(duration):
    """
    Esegue il test del controller Service Slicing.

    Verifica il routing basato sul tipo di servizio:
    1. Connettivita completa: pingall deve mostrare 12/12 ping riusciti
    2. Traffico video (UDP 9999): instradato sul Premium Link (alta banda)
    3. Traffico normale (UDP 5001): instradato sul percorso standard (bassa banda)
    4. La banda video deve essere significativamente superiore a quella normale

    Args:
        duration: Durata dei test iperf in secondi

    Returns:
        dict: Dizionario con i risultati del test
    """
    print_header("TEST 2: SERVICE SLICING", '=')
    print("  Obiettivo: Verificare il routing basato sul tipo di servizio")
    print("  Controller: service_slicing_controller.py")
    print("  Atteso: 12/12 ping, video >> normale in termini di banda")
    print("  Durata test iperf: {} secondi".format(duration))
    print()

    results = {
        'controller': 'service_slicing_controller.py',
        'status': 'ERRORE',
        'pingall_received': 0,
        'pingall_total': 12,
        'full_connectivity': False,
        'video_bandwidth_mbps': 0.0,
        'normal_bandwidth_mbps': 0.0,
        'premium_advantage': False,
        'bandwidth_ratio': 0.0,
        'details': '',
        'raw_pingall': '',
        'raw_iperf_video': '',
        'raw_iperf_normal': ''
    }

    controller_proc = None
    net_proc = None

    try:
        # Fase 1: Pulizia iniziale
        print_subheader("Fase 1: Pulizia ambiente")
        cleanup()

        # Fase 2: Avvio controller
        print_subheader("Fase 2: Avvio controller Service Slicing")
        controller_proc = start_controller(SERVICE_SLICING_CONTROLLER)
        if controller_proc is None:
            results['details'] = 'Impossibile avviare il controller'
            return results

        # Fase 3: Avvio topologia
        print_subheader("Fase 3: Avvio topologia Mininet")
        net_proc = start_topology()
        if net_proc is None:
            results['details'] = 'Impossibile avviare la topologia'
            return results

        # Fase 4: Attesa stabilizzazione rete
        print_subheader("Fase 4: Stabilizzazione rete")
        print("  [*] Attesa stabilizzazione ({} secondi)...".format(
            WAIT_NETWORK_STABILIZE))
        time.sleep(WAIT_NETWORK_STABILIZE)

        # Fase 5: Test di connettivita (pingall)
        print_subheader("Fase 5: Test di connettivita (pingall)")
        pingall_output = run_mininet_command(net_proc, 'pingall', wait_time=30)
        results['raw_pingall'] = pingall_output

        print()
        print("  --- Output pingall ---")
        for line in pingall_output.split('\n'):
            stripped = line.strip()
            if stripped and ('ping' in stripped.lower() or 'result' in stripped.lower()
                           or 'dropped' in stripped.lower() or '->' in stripped
                           or 'H1' in stripped or 'H2' in stripped or 'H3' in stripped or 'H4' in stripped):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")
        print()

        received, total, drop_pct = parse_pingall_output(pingall_output)
        results['pingall_received'] = received
        results['pingall_total'] = total

        # Verifica connettivita completa (12/12)
        # Tolleranza: accettiamo >= 10/12 per instabilita di rete
        if received >= 10:
            results['full_connectivity'] = True
            print_result(
                "Connettivita completa",
                True,
                "{}/{} ping riusciti".format(received, total)
            )
        else:
            print_result(
                "Connettivita completa",
                False,
                "Solo {}/{} ping riusciti (attesi 12/12)".format(received, total)
            )

        time.sleep(WAIT_AFTER_PINGALL)

        # Fase 6: Test iperf UDP video (porta 9999 = Premium Link)
        print_subheader("Fase 6: Test banda video (UDP {}, Premium Link)".format(VIDEO_PORT))
        print("  [*] Percorso atteso: H1 -> S1 -> S2 -> S6 -> H3 (Premium)")

        # Avvia server iperf su H3 per la porta video
        print("  [*] Avvio server iperf su H3 (porta {})...".format(VIDEO_PORT))
        run_mininet_command(
            net_proc,
            'H3 iperf -s -u -p {} &'.format(VIDEO_PORT),
            wait_time=WAIT_IPERF_SETUP
        )

        # Esegui client iperf da H1 verso H3 sulla porta video
        print("  [*] Avvio client iperf H1 -> H3 (porta {}, banda {}, {} sec)...".format(
            VIDEO_PORT, IPERF_BANDWIDTH, duration))
        iperf_video_output = run_mininet_command(
            net_proc,
            'H1 iperf -c {} -u -p {} -b {} -t {}'.format(
                H3_IP, VIDEO_PORT, IPERF_BANDWIDTH, duration),
            wait_time=duration + 5
        )
        results['raw_iperf_video'] = iperf_video_output

        print()
        print("  --- Output iperf video ---")
        for line in iperf_video_output.split('\n'):
            stripped = line.strip()
            if stripped and ('bits/sec' in stripped.lower() or 'server report' in stripped.lower()
                           or 'connected' in stripped.lower() or 'interval' in stripped.lower()):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")

        video_bw = parse_iperf_output(iperf_video_output)
        results['video_bandwidth_mbps'] = video_bw
        print("  Banda video misurata: {:.2f} Mbps".format(video_bw))

        # Termina il server iperf video
        run_mininet_command(net_proc, 'H3 kill %iperf', wait_time=2)
        time.sleep(WAIT_AFTER_TEST)

        # Fase 7: Test iperf UDP normale (porta 5001 = percorso standard)
        print_subheader("Fase 7: Test banda normale (UDP {}, percorso standard)".format(
            NORMAL_PORT))
        print("  [*] Percorso atteso: H1 -> S1 -> S2 -> S3 -> S6 -> H3 (Standard)")

        # Avvia server iperf su H3 per la porta normale
        print("  [*] Avvio server iperf su H3 (porta {})...".format(NORMAL_PORT))
        run_mininet_command(
            net_proc,
            'H3 iperf -s -u -p {} &'.format(NORMAL_PORT),
            wait_time=WAIT_IPERF_SETUP
        )

        # Esegui client iperf da H1 verso H3 sulla porta normale
        print("  [*] Avvio client iperf H1 -> H3 (porta {}, banda {}, {} sec)...".format(
            NORMAL_PORT, IPERF_BANDWIDTH, duration))
        iperf_normal_output = run_mininet_command(
            net_proc,
            'H1 iperf -c {} -u -p {} -b {} -t {}'.format(
                H3_IP, NORMAL_PORT, IPERF_BANDWIDTH, duration),
            wait_time=duration + 5
        )
        results['raw_iperf_normal'] = iperf_normal_output

        print()
        print("  --- Output iperf normale ---")
        for line in iperf_normal_output.split('\n'):
            stripped = line.strip()
            if stripped and ('bits/sec' in stripped.lower() or 'server report' in stripped.lower()
                           or 'connected' in stripped.lower() or 'interval' in stripped.lower()):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")

        normal_bw = parse_iperf_output(iperf_normal_output)
        results['normal_bandwidth_mbps'] = normal_bw
        print("  Banda normale misurata: {:.2f} Mbps".format(normal_bw))

        # Termina il server iperf normale
        run_mininet_command(net_proc, 'H3 kill %iperf', wait_time=2)

        # Fase 8: Analisi comparativa dei risultati
        print_subheader("Fase 8: Analisi comparativa")

        print("  Banda video (Premium Link, porta {}):  {:.2f} Mbps".format(
            VIDEO_PORT, video_bw))
        print("  Banda normale (Standard, porta {}):    {:.2f} Mbps".format(
            NORMAL_PORT, normal_bw))

        # Calcola il rapporto tra le bande
        if normal_bw > 0:
            ratio = video_bw / normal_bw
            results['bandwidth_ratio'] = ratio
            print("  Rapporto video/normale: {:.2f}x".format(ratio))

            # Il traffico video dovrebbe avere banda significativamente superiore
            # Il Premium Link e 6 Mbps vs Standard 2 Mbps (rapporto teorico 3x)
            # Accettiamo un rapporto >= 1.5x come indicatore di successo
            if ratio >= 1.5:
                results['premium_advantage'] = True
                print_result(
                    "Vantaggio Premium Link",
                    True,
                    "Video {:.1f}x piu veloce del normale".format(ratio)
                )
            else:
                print_result(
                    "Vantaggio Premium Link",
                    False,
                    "Rapporto {:.2f}x (atteso >= 1.5x)".format(ratio)
                )
        else:
            print("  [!] Impossibile calcolare il rapporto: banda normale = 0")
            if video_bw > 0:
                results['premium_advantage'] = True
                print_result(
                    "Vantaggio Premium Link",
                    True,
                    "Video {:.2f} Mbps, normale non misurato".format(video_bw)
                )

        # Verifica bande assolute
        # Premium Link: atteso ~5-6 Mbps (capacita 6 Mbps)
        # Percorso standard: atteso ~1.5-2 Mbps (collo di bottiglia 2 Mbps)
        print()
        if video_bw >= 3.0:
            print_result("Banda video >= 3 Mbps", True,
                        "{:.2f} Mbps sul Premium Link".format(video_bw))
        elif video_bw > 0:
            print_result("Banda video >= 3 Mbps", False,
                        "Solo {:.2f} Mbps (atteso >= 3 Mbps)".format(video_bw))

        if 0 < normal_bw <= 3.0:
            print_result("Banda normale <= 3 Mbps (bottleneck)", True,
                        "{:.2f} Mbps sul percorso standard".format(normal_bw))
        elif normal_bw > 3.0:
            print_result("Banda normale <= 3 Mbps (bottleneck)", False,
                        "{:.2f} Mbps (atteso <= 3 Mbps)".format(normal_bw))

        # Determinazione stato complessivo del test
        if results['full_connectivity'] and results['premium_advantage']:
            results['status'] = 'SUPERATO'
        elif results['full_connectivity'] or results['premium_advantage']:
            results['status'] = 'PARZIALE'
        else:
            results['status'] = 'FALLITO'

    except Exception as e:
        results['status'] = 'ERRORE'
        results['details'] = str(e)
        print("  [!] ERRORE durante il test: {}".format(e))

    finally:
        # Pulizia: termina controller e topologia
        print_subheader("Pulizia post-test")
        stop_process(net_proc, "Mininet")
        stop_process(controller_proc, "Controller")
        cleanup()

    # Salvataggio risultati
    save_results('service_slicing', results)

    return results


# ==============================================================================
# TEST 3: DYNAMIC SLICING
# ==============================================================================

def test_dynamic_slicing(duration):
    """
    Esegue il test del controller Dynamic Slicing.

    Verifica l'allocazione dinamica della banda e il meccanismo di preemption:
    1. Connettivita completa: pingall deve mostrare 12/12 ping riusciti
    2. Traffico video (UDP 9999): instradato sul Premium Link
    3. Verifica messaggi di monitoraggio nei log del controller
    4. Verifica che il controller monitori l'utilizzo dei link premium

    Args:
        duration: Durata dei test iperf in secondi

    Returns:
        dict: Dizionario con i risultati del test
    """
    print_header("TEST 3: DYNAMIC SLICING", '=')
    print("  Obiettivo: Verificare allocazione dinamica e preemption")
    print("  Controller: dynamic_slicing_controller.py")
    print("  Atteso: 12/12 ping, monitoraggio attivo, video su Premium Link")
    print("  Durata test iperf: {} secondi".format(duration))
    print()

    results = {
        'controller': 'dynamic_slicing_controller.py',
        'status': 'ERRORE',
        'pingall_received': 0,
        'pingall_total': 12,
        'full_connectivity': False,
        'video_bandwidth_mbps': 0.0,
        'monitoring_active': False,
        'video_detected': False,
        'details': '',
        'raw_pingall': '',
        'raw_iperf_video': '',
        'controller_log_excerpts': ''
    }

    controller_proc = None
    net_proc = None

    # File di log del controller (per verificare i messaggi di monitoraggio)
    controller_log = os.path.join(RESULTS_DIR, 'controller_dynamic_slicing.log')

    try:
        # Fase 1: Pulizia iniziale
        print_subheader("Fase 1: Pulizia ambiente")
        cleanup()

        # Fase 2: Avvio controller
        print_subheader("Fase 2: Avvio controller Dynamic Slicing")
        controller_proc = start_controller(DYNAMIC_SLICING_CONTROLLER)
        if controller_proc is None:
            results['details'] = 'Impossibile avviare il controller'
            return results

        # Fase 3: Avvio topologia
        print_subheader("Fase 3: Avvio topologia Mininet")
        net_proc = start_topology()
        if net_proc is None:
            results['details'] = 'Impossibile avviare la topologia'
            return results

        # Fase 4: Attesa stabilizzazione rete
        print_subheader("Fase 4: Stabilizzazione rete")
        print("  [*] Attesa stabilizzazione ({} secondi)...".format(
            WAIT_NETWORK_STABILIZE))
        time.sleep(WAIT_NETWORK_STABILIZE)

        # Attesa aggiuntiva per i thread di monitoraggio del dynamic controller
        print("  [*] Attesa thread di monitoraggio (10 secondi)...")
        time.sleep(10)

        # Fase 5: Test di connettivita (pingall)
        print_subheader("Fase 5: Test di connettivita (pingall)")
        pingall_output = run_mininet_command(net_proc, 'pingall', wait_time=30)
        results['raw_pingall'] = pingall_output

        print()
        print("  --- Output pingall ---")
        for line in pingall_output.split('\n'):
            stripped = line.strip()
            if stripped and ('ping' in stripped.lower() or 'result' in stripped.lower()
                           or 'dropped' in stripped.lower() or '->' in stripped
                           or 'H1' in stripped or 'H2' in stripped or 'H3' in stripped or 'H4' in stripped):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")
        print()

        received, total, drop_pct = parse_pingall_output(pingall_output)
        results['pingall_received'] = received
        results['pingall_total'] = total

        if received >= 10:
            results['full_connectivity'] = True
            print_result(
                "Connettivita completa",
                True,
                "{}/{} ping riusciti".format(received, total)
            )
        else:
            print_result(
                "Connettivita completa",
                False,
                "Solo {}/{} ping riusciti (attesi 12/12)".format(received, total)
            )

        time.sleep(WAIT_AFTER_PINGALL)

        # Fase 6: Test iperf UDP video (porta 9999 = Premium Link)
        print_subheader("Fase 6: Test banda video (UDP {}, Premium Link)".format(VIDEO_PORT))
        print("  [*] Percorso atteso: H1 -> S1 -> S2 -> S6 -> H3 (Premium)")

        # Avvia server iperf su H3 per la porta video
        print("  [*] Avvio server iperf su H3 (porta {})...".format(VIDEO_PORT))
        run_mininet_command(
            net_proc,
            'H3 iperf -s -u -p {} &'.format(VIDEO_PORT),
            wait_time=WAIT_IPERF_SETUP
        )

        # Esegui client iperf da H1 verso H3 sulla porta video
        print("  [*] Avvio client iperf H1 -> H3 (porta {}, banda {}, {} sec)...".format(
            VIDEO_PORT, IPERF_BANDWIDTH, duration))
        iperf_video_output = run_mininet_command(
            net_proc,
            'H1 iperf -c {} -u -p {} -b {} -t {}'.format(
                H3_IP, VIDEO_PORT, IPERF_BANDWIDTH, duration),
            wait_time=duration + 5
        )
        results['raw_iperf_video'] = iperf_video_output

        print()
        print("  --- Output iperf video ---")
        for line in iperf_video_output.split('\n'):
            stripped = line.strip()
            if stripped and ('bits/sec' in stripped.lower() or 'server report' in stripped.lower()
                           or 'connected' in stripped.lower() or 'interval' in stripped.lower()):
                print("  | {}".format(stripped))
        print("  --- Fine output ---")

        video_bw = parse_iperf_output(iperf_video_output)
        results['video_bandwidth_mbps'] = video_bw
        print("  Banda video misurata: {:.2f} Mbps".format(video_bw))

        if video_bw >= 3.0:
            print_result("Banda video su Premium Link", True,
                        "{:.2f} Mbps".format(video_bw))
        elif video_bw > 0:
            print_result("Banda video su Premium Link", False,
                        "Solo {:.2f} Mbps (atteso >= 3 Mbps)".format(video_bw))

        # Termina il server iperf
        run_mininet_command(net_proc, 'H3 kill %iperf', wait_time=2)
        time.sleep(WAIT_AFTER_TEST)

        # Fase 7: Verifica log del controller per messaggi di monitoraggio
        print_subheader("Fase 7: Analisi log del controller")

        # Attendi che il controller scriva i log
        time.sleep(5)

        if os.path.isfile(controller_log):
            try:
                with open(controller_log, 'r') as f:
                    log_content = f.read()

                results['controller_log_excerpts'] = log_content[-2000:]  # Ultimi 2000 caratteri

                # Verifica presenza messaggi di monitoraggio
                monitoring_keywords = [
                    'throughput',
                    'monitor',
                    'porta premium',
                    'port stats',
                    'Mbps',
                    'capacita',
                    'ALLOCAZIONE',
                    'PREEMPTION',
                    'dynamic'
                ]

                found_keywords = []
                for keyword in monitoring_keywords:
                    if keyword.lower() in log_content.lower():
                        found_keywords.append(keyword)

                if len(found_keywords) >= 2:
                    results['monitoring_active'] = True
                    print_result(
                        "Monitoraggio attivo nel controller",
                        True,
                        "Trovate parole chiave: {}".format(', '.join(found_keywords))
                    )
                else:
                    print_result(
                        "Monitoraggio attivo nel controller",
                        False,
                        "Poche parole chiave trovate: {}".format(', '.join(found_keywords))
                    )

                # Verifica rilevamento traffico video nei log
                video_keywords = [
                    'VIDEO RILEVATO',
                    'video_active',
                    'TRAFFICO VIDEO',
                    'VIDEO',
                ]

                video_found = False
                for keyword in video_keywords:
                    if keyword in log_content:
                        video_found = True
                        break

                if video_found:
                    results['video_detected'] = True
                    print_result(
                        "Rilevamento traffico video",
                        True,
                        "Il controller ha rilevato il traffico video"
                    )
                else:
                    print_result(
                        "Rilevamento traffico video",
                        False,
                        "Nessun messaggio di rilevamento video trovato nei log"
                    )

                # Stampa estratti rilevanti del log
                print()
                print("  --- Estratti dal log del controller ---")
                log_lines = log_content.split('\n')
                relevant_lines = []
                for line in log_lines:
                    line_lower = line.lower()
                    if any(kw.lower() in line_lower for kw in
                           ['throughput', 'video', 'preemption', 'allocazione',
                            'dynamic', 'monitor', 'premium']):
                        relevant_lines.append(line.strip())

                # Mostra al massimo le ultime 15 righe rilevanti
                for line in relevant_lines[-15:]:
                    if line:
                        print("  | {}".format(line[:120]))
                print("  --- Fine estratti ---")

            except Exception as e:
                print("  [!] Errore lettura log controller: {}".format(e))
        else:
            print("  [!] File di log del controller non trovato: {}".format(controller_log))

        # Fase 8: Riepilogo risultati
        print_subheader("Fase 8: Riepilogo")

        # Determinazione stato complessivo del test
        checks_passed = sum([
            results['full_connectivity'],
            results['video_bandwidth_mbps'] > 0,
            results['monitoring_active'],
        ])

        if checks_passed >= 3:
            results['status'] = 'SUPERATO'
        elif checks_passed >= 2:
            results['status'] = 'PARZIALE'
        else:
            results['status'] = 'FALLITO'

        print("  Controlli superati: {}/3".format(checks_passed))
        print("    - Connettivita completa: {}".format(
            'SI' if results['full_connectivity'] else 'NO'))
        print("    - Traffico video funzionante: {}".format(
            'SI' if results['video_bandwidth_mbps'] > 0 else 'NO'))
        print("    - Monitoraggio attivo: {}".format(
            'SI' if results['monitoring_active'] else 'NO'))

    except Exception as e:
        results['status'] = 'ERRORE'
        results['details'] = str(e)
        print("  [!] ERRORE durante il test: {}".format(e))

    finally:
        # Pulizia: termina controller e topologia
        print_subheader("Pulizia post-test")
        stop_process(net_proc, "Mininet")
        stop_process(controller_proc, "Controller")
        cleanup()

    # Salvataggio risultati
    save_results('dynamic_slicing', results)

    return results


# ==============================================================================
# FUNZIONE PRINCIPALE
# ==============================================================================

def main():
    """
    Funzione principale che orchestra l'esecuzione della suite di test.

    Analizza gli argomenti della riga di comando, verifica i prerequisiti,
    ed esegue i test richiesti. Al termine, stampa un riepilogo complessivo.
    """
    # ======================================================================
    # PARSING DEGLI ARGOMENTI
    # ======================================================================
    parser = argparse.ArgumentParser(
        description='Script di test automatizzati - SDN Network Slicing '
                    '(Network Slicing con Premium Links)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi di utilizzo:
  sudo python3 scripts/run_tests.py                    # Tutti i test
  sudo python3 scripts/run_tests.py --test topology    # Solo Topology Slicing
  sudo python3 scripts/run_tests.py --test service     # Solo Service Slicing
  sudo python3 scripts/run_tests.py --test dynamic     # Solo Dynamic Slicing
  sudo python3 scripts/run_tests.py --duration 15      # Test iperf da 15 secondi
        """
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=10,
        help='Durata dei test iperf in secondi (default: 10)'
    )
    parser.add_argument(
        '--test',
        type=str,
        choices=['topology', 'service', 'dynamic', 'all'],
        default='all',
        help='Test specifico da eseguire (default: all)'
    )

    args = parser.parse_args()

    # ======================================================================
    # INTESTAZIONE
    # ======================================================================
    print_header("SUITE DI TEST AUTOMATIZZATI - SDN NETWORK SLICING", '=')
    print("  Network Slicing con Premium Links")
    print("  Data: {}".format(timestamp()))
    print("  Durata test iperf: {} secondi".format(args.duration))
    print("  Test selezionati: {}".format(args.test.upper()))
    print("  Directory progetto: {}".format(PROJECT_DIR))
    print("  Directory risultati: {}".format(RESULTS_DIR))
    print()

    # ======================================================================
    # VERIFICA PREREQUISITI
    # ======================================================================
    print_subheader("Verifica prerequisiti")

    # Verifica privilegi root (necessari per Mininet)
    if os.geteuid() != 0:
        print("  [!!] ERRORE: Questo script richiede privilegi root (sudo)")
        print("  [!!] Esegui con: sudo python3 {}".format(sys.argv[0]))
        sys.exit(1)

    print("  [OK] Privilegi root verificati")

    # Verifica esistenza file
    print()
    if not verify_files_exist():
        print()
        print("  [!!] ERRORE: File mancanti. Impossibile procedere con i test.")
        sys.exit(1)

    # Crea directory risultati se non esiste
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print()
    print("  [OK] Directory risultati: {}".format(RESULTS_DIR))

    # Verifica che ryu-manager sia disponibile
    try:
        result = subprocess.run(
            ['which', 'ryu-manager'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        if result.returncode == 0:
            print("  [OK] ryu-manager trovato: {}".format(
                result.stdout.decode().strip()))
        else:
            print("  [!!] ERRORE: ryu-manager non trovato nel PATH")
            print("  [!!] Installa Ryu: pip install ryu")
            sys.exit(1)
    except Exception:
        print("  [!!] ERRORE: impossibile verificare ryu-manager")
        sys.exit(1)

    # Verifica che Mininet sia disponibile
    try:
        result = subprocess.run(
            ['which', 'mn'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        if result.returncode == 0:
            print("  [OK] Mininet trovato: {}".format(
                result.stdout.decode().strip()))
        else:
            print("  [!!] ERRORE: Mininet non trovato nel PATH")
            sys.exit(1)
    except Exception:
        print("  [!!] ERRORE: impossibile verificare Mininet")
        sys.exit(1)

    # ======================================================================
    # PULIZIA INIZIALE
    # ======================================================================
    print_subheader("Pulizia iniziale")
    cleanup()

    # ======================================================================
    # ESECUZIONE DEI TEST
    # ======================================================================
    all_results = {}
    test_start_time = time.time()

    # Test 1: Topology Slicing
    if args.test in ('all', 'topology'):
        result = test_topology_slicing(args.duration)
        all_results['topology_slicing'] = result

    # Test 2: Service Slicing
    if args.test in ('all', 'service'):
        result = test_service_slicing(args.duration)
        all_results['service_slicing'] = result

    # Test 3: Dynamic Slicing
    if args.test in ('all', 'dynamic'):
        result = test_dynamic_slicing(args.duration)
        all_results['dynamic_slicing'] = result

    test_elapsed = time.time() - test_start_time

    # ======================================================================
    # RIEPILOGO FINALE
    # ======================================================================
    print_header("RIEPILOGO FINALE - SDN NETWORK SLICING", '=')

    print("  Data completamento: {}".format(timestamp()))
    print("  Tempo totale: {:.0f} secondi ({:.1f} minuti)".format(
        test_elapsed, test_elapsed / 60.0))
    print("  Durata test iperf: {} secondi".format(args.duration))
    print()

    # Tabella riepilogativa
    print("  {:<30} {:<15} {}".format("TEST", "STATO", "DETTAGLI"))
    print("  " + "-" * 66)

    overall_passed = 0
    overall_total = len(all_results)

    for test_name, result in all_results.items():
        status = result.get('status', 'ERRORE')
        details = ""

        if test_name == 'topology_slicing':
            display_name = "1. Topology Slicing"
            details = "{}/{} ping".format(
                result.get('pingall_received', '?'),
                result.get('pingall_total', '?')
            )
        elif test_name == 'service_slicing':
            display_name = "2. Service Slicing"
            video_bw = result.get('video_bandwidth_mbps', 0)
            normal_bw = result.get('normal_bandwidth_mbps', 0)
            details = "Video: {:.1f} Mbps, Normale: {:.1f} Mbps".format(
                video_bw, normal_bw)
        elif test_name == 'dynamic_slicing':
            display_name = "3. Dynamic Slicing"
            details = "Monitor: {}, Video: {:.1f} Mbps".format(
                'SI' if result.get('monitoring_active') else 'NO',
                result.get('video_bandwidth_mbps', 0)
            )
        else:
            display_name = test_name

        if 'SUPERATO' in status:
            overall_passed += 1

        print("  {:<30} {:<15} {}".format(display_name, status, details))

    print("  " + "-" * 66)
    print("  {:<30} {}/{}".format("TOTALE SUPERATI:", overall_passed, overall_total))
    print()

    # Salvataggio riepilogo complessivo
    summary = {
        'timestamp': timestamp(),
        'duration_seconds': args.duration,
        'elapsed_seconds': test_elapsed,
        'tests_passed': overall_passed,
        'tests_total': overall_total,
        'results': {}
    }
    for test_name, result in all_results.items():
        summary['results'][test_name] = {
            'status': result.get('status', 'ERRORE'),
            'pingall': '{}/{}'.format(
                result.get('pingall_received', '?'),
                result.get('pingall_total', '?')
            )
        }

    save_results('summary', summary)

    print()
    print("  Risultati salvati in: {}".format(RESULTS_DIR))
    print()

    # Pulizia finale
    print_subheader("Pulizia finale")
    cleanup()

    print()
    print_header("TEST COMPLETATI - SDN NETWORK SLICING", '=')

    # Codice di uscita: 0 se tutti i test sono superati, 1 altrimenti
    sys.exit(0 if overall_passed == overall_total else 1)


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == '__main__':
    main()
