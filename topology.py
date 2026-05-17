#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Topologia Mininet per il progetto di Network Slicing.

6 switch OpenFlow, 4 host, due percorsi paralleli (upper/lower).
I Premium Links S2→S6 e S4→S6 vengono usati nelle fasi 2 e 3.
La fase 1 (topology slicing) li ignora e usa solo i percorsi standard.

Schema della rete:

                      +---- S2 ==== S3 ----+
        H1 --+      |      \\             |
               +- S1 -+       \\ Premium   +- S6 --+-- H3
        H2 --+      |         \\         |       |
                      +---- S4 ====\\S5 ---+       +-- H4
                                \\   |
                                 \\--+

Dove:
    ====  indica i link standard (2 Mbps, 50ms di ritardo)
    \\    indica i Premium Links (6 Mbps, 3ms di ritardo)

PERCORSI DISPONIBILI
--------------------

Upper Slice (H1 <-> H3):
    - Percorso standard: H1 -> S1 -> S2 -> S3 -> S6 -> H3
      Caratteristiche: 2 Mbps al collo di bottiglia, ~110ms di latenza
    - Percorso premium:  H1 -> S1 -> S2 -> S6 -> H3
      Caratteristiche: 6 Mbps, ~13ms di latenza (bypassa S3)

Lower Slice (H2 <-> H4):
    - Percorso standard: H2 -> S1 -> S4 -> S5 -> S6 -> H4
      Caratteristiche: 2 Mbps al collo di bottiglia, ~110ms di latenza
    - Percorso premium:  H2 -> S1 -> S4 -> S6 -> H4
      Caratteristiche: 6 Mbps, ~13ms di latenza (bypassa S5)

I Premium Links offrono un guadagno significativo:
    - Banda 3x superiore (6 Mbps vs 2 Mbps)
    - Latenza ~8.5x inferiore (13ms vs 110ms)
    - Un hop in meno (3 vs 4 nel percorso)

RUOLO DEGLI SWITCH
------------------

    S1 - Hub di Ingresso
         Raccoglie il traffico dai server (H1, H2) e lo smista verso lo slice
         appropriato (upper o lower).

    S2 - Nodo Upper Slice
         Punto di decisione per il traffico dell'upper slice. Da qui il
         traffico puo seguire il percorso standard (porta 2, verso S3)
         oppure il Premium Link (porta 3, direttamente verso S6).

    S3 - Collo di Bottiglia Upper
         Switch di transito sul percorso standard dell'upper slice.
         I link che lo attraversano sono limitati a 2 Mbps con 50ms
         di ritardo, creando il bottleneck che il Premium Link bypassa.

    S4 - Nodo Lower Slice
         Equivalente di S2 per il lower slice.

    S5 - Collo di Bottiglia Lower
         Equivalente di S3 per il lower slice.

    S6 - Hub di Distribuzione
         Punto di convergenza di tutti i percorsi. Consegna il traffico
         ai client finali H3 e H4.

PREREQUISITI
------------

    - Mininet 2.3.0+, Open vSwitch con OpenFlow 1.3
    - Controller SDN in ascolto su 127.0.0.1:6653
    - Python 3.8+

UTILIZZO
--------

    ryu-manager <controller>.py          # Terminale 1
    sudo python3 topology.py             # Terminale 2

================================================================================
"""

# Inizio importando da Mininet le classi necessarie
from mininet.topo import Topo  # Classe base per topologia
from mininet.net import Mininet  # Oggetto "rete" da istanziare
from mininet.node import OVSKernelSwitch, RemoteController  # Tipi di switch e controller
from mininet.cli import CLI  # prompt interattivo mininet
from mininet.link import TCLink  # Link con traffic control (banda + delay)  
from mininet.log import setLogLevel, info  # Logging

# TCLink = "Traffic Control Link" è il link che permette di specificare banda e Delay. Senza TCLink, i link Mininet sono veth-pair "nudi" senza limiti di banda.
# RemoteController = dice a Mininet "non gestire tu il controllo ma connettiti a un controller esterni su un certo IP.porta". È così che si collega a Ryu.


# =============================================================================
# COSTANTI DI CONFIGURAZIONE
# =============================================================================

# Indirizzi MAC degli host
# Assegnati staticamente per garantire coerenza con le regole OpenFlow
# dei controller. Non utilizzare autoSetMacs per evitare conflitti.
H1_MAC = '00:00:00:00:00:01'
H2_MAC = '00:00:00:00:00:02'
H3_MAC = '00:00:00:00:00:03'
H4_MAC = '00:00:00:00:00:04'

# i MAC sono fissi per essere coerenti con le regole OpenFlow dei controller. I controller matchano sul MAC -> se Mininet assegnasse MAC random ogni volta, le regole non matcherebbero più. Quindi, per lo stesso   # motivo anche più sotto si usa autoSetMacs=False.   

# Parametri di banda (Mbps)
ACCESS_BW = 15       # Banda dei link di accesso (H1/H2 -> S1 e S1 -> S2/S4)
STANDARD_BW = 2      # Banda dei link standard (colli di bottiglia S2-S3, S3-S6, S4-S5, S5-S6)
PREMIUM_BW = 6       # Banda dei Premium Links (S2-S6, S4-S6)
DELIVERY_BW = 8      # Banda dei link di consegna (S6 -> H3/H4)

# Parametri di latenza
ACCESS_DELAY = '1ms'      # Latenza link di accesso (H1/H2 -> S1)
DIST_DELAY = '5ms'        # Latenza link di distribuzione (S1 -> S2 e S1 -> S4)
STANDARD_DELAY = '50ms'   # Latenza link standard (colli di bottiglia)
PREMIUM_DELAY = '3ms'     # Latenza Premium Links
DELIVERY_DELAY = '5ms'    # Latenza link di consegna

# La dissimetria è voluta : i link di accesso e distribuzione (H1-H2 -> S1 e S1 -> S2 e S1 -> S4) sono larghi (15 Mbps), così il collo di bottiglia non è lì. Il collo di bottiglia è a cavallo di S3 e S5, gli      # switch "lenti". I Premium Links (S2>S6 e S4->S6) bypassano proprio S3 e S5.


# =============================================================================
# DEFINIZIONE DELLA TOPOLOGIA
# =============================================================================

class PremiumLinkTopology(Topo):  # Eredita da classe Topo e ridefinisce un solo metodo build(). Mininet chiama build() quando si istanzia la topologia e dentro build() si costruiscono host, switch e link.
    """
    Topologia di rete a 6 switch con Premium Links per Network Slicing.

    Ogni link viene creato con porte esplicitamente assegnate (port1, port2)
    per garantire che il mapping corrisponda a quello atteso dai controller.

    Port Mapping Risultante:

        S1: porta 1 = H1, porta 2 = H2, porta 3 = S2, porta 4 = S4
        S2: porta 1 = S1, porta 2 = S3, porta 3 = S6 (PREMIUM)
        S3: porta 1 = S2, porta 2 = S6
        S4: porta 1 = S1, porta 2 = S5, porta 3 = S6 (PREMIUM)
        S5: porta 1 = S4, porta 2 = S6
        S6: porta 1 = S3, porta 2 = S2 (PREMIUM), porta 3 = H3,
            porta 4 = S5, porta 5 = S4 (PREMIUM), porta 6 = H4
    """

    def build(self):
        """
        Costruisce la topologia completa con porte esplicitamente assegnate.
        """
        info('*** Creazione topologia Network Slicing con Premium Links\n')

        # ================================================================
        # CREAZIONE HOST
        # ================================================================
        # MAC address statici per coerenza con le regole dei controller
	# Quattro host nella stessa subnet 10.0.0.0/24, MAC presi dalle costanti. self.addHost(...) restituisce un identificatore che si userà dopo per creare i link.
	
        h1 = self.addHost('H1', ip='10.0.0.1/24', mac=H1_MAC)
        h2 = self.addHost('H2', ip='10.0.0.2/24', mac=H2_MAC)
        h3 = self.addHost('H3', ip='10.0.0.3/24', mac=H3_MAC)
        h4 = self.addHost('H4', ip='10.0.0.4/24', mac=H4_MAC)

        info('*** Host: H1 (10.0.0.1), H2 (10.0.0.2), '
             'H3 (10.0.0.3), H4 (10.0.0.4)\n')

        # ================================================================
        # CREAZIONE SWITCH
        # ================================================================
        # Tutti gli switch usano OpenFlow 1.3 e hanno dpid esplicito
        # per evitare ambiguita nell'identificazione.
	# Sei switch OVS con dpid espliciti (1, 2, ..., 6). Il dpid è il DataPath ID, l'identificatore che OpenFlow usa per riconoscere lo switch. Fissandolo, nel codice dei controller si può scrivere if          	# datapath.id == 2 e sapere che si sta parlando di S2. Stesso principio dei MAC fissi.

        s1 = self.addSwitch('s1', dpid='1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', dpid='2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', dpid='3', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', dpid='4', protocols='OpenFlow13')
        s5 = self.addSwitch('s5', dpid='5', protocols='OpenFlow13')
        s6 = self.addSwitch('s6', dpid='6', protocols='OpenFlow13')

        info('*** Switch: S1-S6 (OpenFlow 1.3)\n')

        # ================================================================
        # UPPER SLICE - Percorso H1 -> H3
        # ================================================================
        # Il percorso standard passa attraverso S3 (collo di bottiglia).
        # Il percorso premium bypassa S3 collegando S2 direttamente a S6.
	# I parametri chiave sono bw e Delay che sono applicati grazie 
	# port1=2, port2=1 assegnano esplicitamente i numeri di porta sui due lati del link. Port1 è la porta di s2, port2 è la porta di S3
	# Quando il controller deve fare OUTPUT=porta 3, deve sapere che la porta 3 di S2 è il Premium Link verso S6 e non un'altra cosa.
	# Se si lascia che Mininet assegni le porte in ordine casuale / incrementale, il mapping cambia tra un'esecuzione e l'altra e i controller si rompono.
	# Assegnare le porte esplicitamente è l'unico modo per avere un contratto stabile tra topologia e controller.
	
        # H1 -> S1 (link di accesso, 15 Mbps, 1ms)
        #   H1 porta 1, S1 porta 1
        self.addLink(h1, s1,
                     bw=ACCESS_BW, delay=ACCESS_DELAY,
                     port1=1, port2=1)	# port1 si riferisce al primo argomento della chiamata, mentre port2 al secondo argomento della chiamata. Quindi port1 si riferisce a H1 e port2 a S1. Entrambi sono collegati sulla loro porta1.

        # S1 -> S2 (distribuzione verso upper slice, 15 Mbps, 5ms)
        #   S1 porta 3, S2 porta 1
        self.addLink(s1, s2,
                     bw=ACCESS_BW, delay=DIST_DELAY,
                     port1=3, port2=1)

        # S2 -> S3 (percorso standard, COLLO DI BOTTIGLIA: 2 Mbps, 50ms)
        #   S2 porta 2, S3 porta 1
        self.addLink(s2, s3,
                     bw=STANDARD_BW, delay=STANDARD_DELAY,
                     port1=2, port2=1)

        # S3 -> S6 (completamento percorso standard, 2 Mbps, 50ms)
        #   S3 porta 2, S6 porta 1
        self.addLink(s3, s6,
                     bw=STANDARD_BW, delay=STANDARD_DELAY,
                     port1=2, port2=1)

        # S6 -> H3 (consegna al client, 8 Mbps, 5ms)
        #   S6 porta 3, H3 porta 1
        self.addLink(s6, h3,
                     bw=DELIVERY_BW, delay=DELIVERY_DELAY,
                     port1=3, port2=1)

        info('*** Upper slice: H1-S1-S2-S3-S6-H3 '
             '(bottleneck 2 Mbps @ S2-S3, S3-S6)\n')

        # ================================================================
        # LOWER SLICE - Percorso H2 -> H4
        # ================================================================
        # Struttura simmetrica all'upper slice. Il percorso standard
        # passa attraverso S5 (collo di bottiglia).

        # H2 -> S1 (link di accesso, 15 Mbps, 1ms)
        #   H2 porta 1, S1 porta 2
        self.addLink(h2, s1,
                     bw=ACCESS_BW, delay=ACCESS_DELAY,
                     port1=1, port2=2)

        # S1 -> S4 (distribuzione verso lower slice, 15 Mbps, 5ms)
        #   S1 porta 4, S4 porta 1
        self.addLink(s1, s4,
                     bw=ACCESS_BW, delay=DIST_DELAY,
                     port1=4, port2=1)

        # S4 -> S5 (percorso standard, COLLO DI BOTTIGLIA: 2 Mbps, 50ms)
        #   S4 porta 2, S5 porta 1
        self.addLink(s4, s5,
                     bw=STANDARD_BW, delay=STANDARD_DELAY,
                     port1=2, port2=1)

        # S5 -> S6 (completamento percorso standard, 2 Mbps, 50ms)
        #   S5 porta 2, S6 porta 4
        self.addLink(s5, s6,
                     bw=STANDARD_BW, delay=STANDARD_DELAY,
                     port1=2, port2=4)

        # S6 -> H4 (consegna al client, 8 Mbps, 5ms)
        #   S6 porta 6, H4 porta 1
        self.addLink(s6, h4,
                     bw=DELIVERY_BW, delay=DELIVERY_DELAY,
                     port1=6, port2=1)

        info('*** Lower slice: H2-S1-S4-S5-S6-H4 '
             '(bottleneck 2 Mbps @ S4-S5, S5-S6)\n')

        # ================================================================
        # PREMIUM LINKS - Bypass dei colli di bottiglia
        # ================================================================
        # I Premium Links collegano direttamente gli switch di decisione
        # (S2, S4) all'hub di distribuzione (S6), bypassando i nodi
        # lenti (S3, S5). Offrono 6 Mbps con solo 3ms di latenza.

        # S2 -> S6 (Premium Link upper, bypassa S3)
        #   S2 porta 3, S6 porta 2
        self.addLink(s2, s6,
                     bw=PREMIUM_BW, delay=PREMIUM_DELAY,
                     port1=3, port2=2)

        # S4 -> S6 (Premium Link lower, bypassa S5)
        #   S4 porta 3, S6 porta 5
        self.addLink(s4, s6,
                     bw=PREMIUM_BW, delay=PREMIUM_DELAY,
                     port1=3, port2=5)

        info('*** Premium Links: S2-S6 e S4-S6 (6 Mbps, 3ms)\n')


# =============================================================================
# PORT MAPPING DI RIFERIMENTO
# =============================================================================
#
# S1 - Hub di Ingresso:
#   Porta 1 -> H1           (link accesso, 15 Mbps, 1ms)
#   Porta 2 -> H2           (link accesso, 15 Mbps, 1ms)
#   Porta 3 -> S2             (distribuzione upper, 15 Mbps, 5ms)
#   Porta 4 -> S4             (distribuzione lower, 15 Mbps, 5ms)
#
# S2 - Nodo Upper Slice:
#   Porta 1 -> S1             (da hub ingresso)
#   Porta 2 -> S3             (percorso standard, 2 Mbps, 50ms)
#   Porta 3 -> S6             (PREMIUM LINK, 6 Mbps, 3ms)
#
# S3 - Collo di Bottiglia Upper:
#   Porta 1 -> S2             (da nodo upper)
#   Porta 2 -> S6             (verso hub distribuzione, 2 Mbps, 50ms)
#
# S4 - Nodo Lower Slice:
#   Porta 1 -> S1             (da hub ingresso)
#   Porta 2 -> S5             (percorso standard, 2 Mbps, 50ms)
#   Porta 3 -> S6             (PREMIUM LINK, 6 Mbps, 3ms)
#
# S5 - Collo di Bottiglia Lower:
#   Porta 1 -> S4             (da nodo lower)
#   Porta 2 -> S6             (verso hub distribuzione, 2 Mbps, 50ms)
#
# S6 - Hub di Distribuzione:
#   Porta 1 -> S3             (upper standard, 2 Mbps)
#   Porta 2 -> S2             (PREMIUM upper, 6 Mbps)
#   Porta 3 -> H3             (consegna client, 8 Mbps)
#   Porta 4 -> S5             (lower standard, 2 Mbps)
#   Porta 5 -> S4             (PREMIUM lower, 6 Mbps)
#   Porta 6 -> H4             (consegna client, 8 Mbps)
# =============================================================================

# S6 ha 6 porte ed è l'unico switch dove l'ordine delle porte non è lineare. Questo perchè S6 è l'hub di distribuzione e si collega a tutti : sia ai percorsi standard (porte 1, 4) sia ai Premium (porte 2, 5) sia  # agli host finali (porte 3, 6). 

def run_topology():
    """
    Funzione principale per avviare la topologia di rete.

    Crea la topologia, la collega al controller SDN remoto su
    127.0.0.1:6653, e apre la CLI interattiva di Mininet.
    """
    setLogLevel('info')

    info('\n')
    info('=' * 65 + '\n')
    info('     NETWORK SLICING CON PREMIUM LINKS - SDN Network Slicing\n')
    info('=' * 65 + '\n')
    info('\n')
    info('Configurazione della rete:\n')
    info('  - 6 switch OpenFlow 1.3 (S1-S6)\n')
    info('  - 4 host: H1, H2, H3, H4\n')
    info('  - Upper Slice: H1 <-> H3\n')
    info('  - Lower Slice: H2 <-> H4\n')
    info('  - Premium Links: 6 Mbps, 3ms (S2-S6, S4-S6)\n')
    info('  - Standard Links: 2 Mbps, 50ms (S2-S3-S6, S4-S5-S6)\n')
    info('\n')

    topo = PremiumLinkTopology()  # Istanzia la topologia (chiama build())

    net = Mininet(
        topo=topo,
        switch=OVSKernelSwitch,  # Usa OVS (non il default)
        controller=RemoteController('c0', ip='127.0.0.1', port=6653),  # Controller ESTERNO
        link=TCLink,  # Link con banda / delay
        autoSetMacs=False  # NON sovrascrivere i MAC
    )

    net.start()  # Accende la rete

    info('\n*** Topologia avviata\n')
    info('*** Controller remoto: 127.0.0.1:6653\n')
    info('\n')
    info('*** Comandi utili:\n')
    info('    pingall                              - Test connettivita\n')
    info('    H3 iperf -s -u -p 9999 &            - Server video\n')
    info('    H1 iperf -c 10.0.0.3 -u -p 9999 -b 5M -t 10  - Test video\n')
    info('    H3 iperf -s -u -p 5001 &            - Server dati\n')
    info('    H1 iperf -c 10.0.0.3 -u -p 5001 -b 5M -t 10  - Test standard\n')
    info('\n')

    CLI(net)  # Apre il prompt mininet>

    net.stop()  # Quando si esce dalla CLI si ferma tutto
    info('*** Rete arrestata\n')


if __name__ == '__main__':
    run_topology()


# Cinque parametri, cinque scelte di design
# 1. topo = topo --> usa la topologia custom che hai costruito. 
# 2. switch=OVSKernelSwitch --> switch Open vSwitch in kernel mode (veloci e supportano OpenFlow 1.3 bene)
# 3. controller=RemoteController(..., port=6653) --> dice a Mininet "per il piano di controllo, connettiti a 127.0.0.1:6653". 6653 è la porta OpenFlow standard ed è quella su cui Ryu ascolta di default quando si  # lancia ryu-manager. SEMPRE PRIMA IL CONTROLLER E POI LA TOPOLOGIA. SE MININET PARTE PRIMA, NON TROVA NESSUNO IN ASCOLTO. *
# 4. link=TCLink --> abilita bw/delay sui link.
# 5. autoSetMacs=False --> non toccare i MAC, tieni quelli dell'AddHost.

# * Mininet si avvia correttamente, la rete viene creata, si ottiene il prompt mininet> ma gli switch OVS provano a connettersi a 127.0.0.1:6653 e non trovano nessuno -> nessun handshake # OpenFlow -> nessuna flow rule installata -> nessuna tale-miss -> gli switch non sanno cosa fare di nessun pacchetto -> pingall da 0% di connettività. Mininet quindi parte, la rete viene # correttamente creata ma è cieca senza controller.

