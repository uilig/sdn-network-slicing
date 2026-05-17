"""
Controller Ryu per la fase 3: Dynamic Slicing con Video Preemption.

Estende la fase 2 aggiungendo allocazione dinamica: quando i Premium Links
sono sotto il 30% di utilizzo e non c'è traffico video, anche il traffico
UDP 800 può usarli (priorità 110). Quando arriva il video (UDP 9999),
le regole dinamiche vengono rimosse immediatamente (preemption).

Il monitoraggio gira su due thread separati: uno raccoglie le port stats
ogni 5s, l'altro decide allocazione/revoca ogni 2s. Il throughput viene
smoothato con media mobile esponenziale per evitare oscillazioni.
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response

import json
import os
import time

try:
    from influxdb import InfluxDBClient as _InfluxDBClient
except ImportError:
    _InfluxDBClient = None

try:
    from eventlet.lock import Semaphore as _Lock
except ImportError:
    from threading import Lock as _Lock

DASHBOARD_APP_INSTANCE_NAME = 'dynamic_slicing_app'


class DynamicSlicingController(app_manager.RyuApp):
    """
    Controller SDN per Dynamic Slicing con Video Preemption.

    Questa classe implementa la logica di controllo per una topologia a 6 switch
    con allocazione dinamica della banda sui link premium e meccanismo di
    preemption per garantire la priorita' del traffico video.

    Il controller utilizza OpenFlow 1.3 e gestisce:
    - Instradamento statico del traffico video sui link premium
    - Instradamento statico del traffico normale sui link standard
    - Allocazione dinamica del traffico normale sui link premium quando sottoutilizzati
    - Preemption (rimozione) del traffico dinamico all'arrivo del video
    - Monitoraggio continuo dell'utilizzo dei link premium
    """

    # ------------------------------------------------------------------ #
    #  Versione OpenFlow supportata                                       #
    # ------------------------------------------------------------------ #
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ------------------------------------------------------------------ #
    #  Contesto WSGI per esporre REST API alla dashboard                  #
    # ------------------------------------------------------------------ #
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        """
        Inizializzazione del controller.

        Vengono definite tutte le costanti, la mappa delle porte per ogni switch,
        lo stato dei link premium e le strutture dati per il tracciamento dei
        flussi dinamici e video.
        """
        super(DynamicSlicingController, self).__init__(*args, **kwargs)

        # ============================================================== #
        #  Registrazione WSGI per REST API della dashboard               #
        # ============================================================== #
        wsgi = kwargs['wsgi']
        wsgi.register(DashboardAPI, {DASHBOARD_APP_INSTANCE_NAME: self})

        # ============================================================== #
        #  VARIABILI DI STATO PER LA DASHBOARD                          #
        # ============================================================== #
        self.preemption_count = 0
        self.dashboard_events = []
        self.MAX_EVENTS = 50
        self.stats_lock = _Lock()
        self.port_stats = {}
        self.port_speed = {}

        # Client InfluxDB per storicizzazione metriche (Grafana)
        self.influx = None
        if _InfluxDBClient is not None:
            try:
                self.influx = _InfluxDBClient('localhost', 8086,
                                              database='sdn_metrics')
                self.influx.ping()
                self.logger.info("InfluxDB connesso (database: sdn_metrics)")
            except Exception as e:
                self.logger.warning("InfluxDB non raggiungibile: %s", e)
                self.influx = None

        # ============================================================== #
        #  COSTANTI: Indirizzi MAC degli host nella topologia            #
        # ============================================================== #
        # H1 e H2 sono le sorgenti di traffico (content delivery)
        self.H1_MAC = '00:00:00:00:00:01'
        self.H2_MAC = '00:00:00:00:00:02'
        # H3 e H4 sono le destinazioni (client)
        self.H3_MAC = '00:00:00:00:00:03'
        self.H4_MAC = '00:00:00:00:00:04'

        # ============================================================== #
        #  COSTANTI: Porte UDP per identificare i tipi di traffico       #
        # ============================================================== #
        # Porta UDP 9999: traffico video ad alta priorita'
        self.VIDEO_UDP_PORT = 9999
        # Porta UDP 800: traffico normale che puo' essere allocato dinamicamente
        self.DYNAMIC_UDP_PORT = 800

        # ============================================================== #
        #  COSTANTI: Tipi Ethernet e protocollo IP                       #
        # ============================================================== #
        self.ETH_TYPE_IP = 0x0800    # IPv4
        self.ETH_TYPE_ARP = 0x0806   # ARP (Address Resolution Protocol)
        self.IP_PROTO_UDP = 17       # Protocollo UDP nel campo protocol di IPv4

        # ============================================================== #
        #  COSTANTI: Livelli di priorita' per le regole OpenFlow         #
        # ============================================================== #
        # Priorita' piu' alta: il traffico video ha sempre la precedenza
        self.PRIORITY_VIDEO = 200
        # Priorita' dinamica: superiore a quella di default (100) cosi' le regole
        # dinamiche instradano UDP 800 sul premium link quando sono attive.
        # Inferiore a PRIORITY_VIDEO (200) cosi' il video ha sempre la precedenza.
        # Alla rimozione (preemption), il traffico ricade sulle regole default (100).
        self.PRIORITY_DYNAMIC = 110
        # Priorita' di default per il traffico normale sulle rotte standard
        self.PRIORITY_DEFAULT = 100
        # Priorita' piu' bassa per il traffico ARP
        self.PRIORITY_ARP = 50

        # ============================================================== #
        #  COSTANTI: Soglie di monitoraggio e capacita' dei link         #
        # ============================================================== #
        # Capacita' massima del link premium in Megabit al secondo
        self.PREMIUM_LINK_CAPACITY_MBPS = 6.0
        # Soglia sotto la quale il link premium e' considerato "libero"
        # e puo' accogliere traffico dinamico (30% della capacita')
        self.BANDWIDTH_THRESHOLD = 0.30
        # Soglia sopra la quale scatta la preemption del traffico dinamico
        # (80% della capacita')
        self.PREEMPTION_THRESHOLD = 0.80
        # Intervallo in secondi tra le richieste di statistiche delle porte
        self.MONITOR_INTERVAL = 5
        # Intervallo in secondi tra i controlli per allocazione/revoca dinamica
        self.DYNAMIC_CHECK_INTERVAL = 2

        # ============================================================== #
        #  MAPPA DELLE PORTE: Associazione nome logico -> numero porta   #
        # ============================================================== #
        # Ogni switch ha una mappa che associa nomi descrittivi ai numeri
        # di porta fisici. Questo rende il codice piu' leggibile e
        # manutenibile rispetto all'uso diretto dei numeri.
        self.PORTS = {
            # Switch 1 (S1): switch di ingresso, connesso alle sorgenti H1 e H2
            1: {
                'H1': 1,         # Porta verso il server H1 (h1)
                'H2': 2,         # Porta verso il server H2 (h2)
                'to_upper': 3,     # Porta verso il percorso superiore (S2)
                'to_lower': 4      # Porta verso il percorso inferiore (S4)
            },
            # Switch 2 (S2): switch del percorso superiore con link premium
            2: {
                'to_s1': 1,             # Porta verso S1
                'to_s3_standard': 2,    # Porta verso S3 (percorso standard)
                'to_s6_premium': 3      # Porta verso S6 (link premium diretto)
            },
            # Switch 3 (S3): switch intermedio del percorso standard superiore
            3: {
                'to_s2': 1,    # Porta verso S2
                'to_s6': 2     # Porta verso S6
            },
            # Switch 4 (S4): switch del percorso inferiore con link premium
            4: {
                'to_s1': 1,             # Porta verso S1
                'to_s5_standard': 2,    # Porta verso S5 (percorso standard)
                'to_s6_premium': 3      # Porta verso S6 (link premium diretto)
            },
            # Switch 5 (S5): switch intermedio del percorso standard inferiore
            5: {
                'to_s4': 1,    # Porta verso S4
                'to_s6': 2     # Porta verso S6
            },
            # Switch 6 (S6): switch di uscita, connesso ai client H3 e H4
            6: {
                'from_s3_standard': 1,   # Porta da S3 (standard superiore)
                'from_s2_premium': 2,    # Porta da S2 (premium superiore)
                'H3': 3,                 # Porta verso il client H3 (h3)
                'from_s5_standard': 4,   # Porta da S5 (standard inferiore)
                'from_s4_premium': 5,    # Porta da S4 (premium inferiore)
                'H4': 6                  # Porta verso il client H4 (h4)
            }
        }

        # ============================================================== #
        #  STATO DEI LINK PREMIUM: Tracciamento in tempo reale           #
        # ============================================================== #
        # Per ogni switch con link premium (S2 e S4), teniamo traccia di:
        # - port: il numero di porta del link premium
        # - usage_mbps: l'utilizzo corrente stimato in Mbps
        # - capacity_mbps: la capacita' massima del link
        # - dynamic_active: se ci sono regole dinamiche installate
        # - video_active: se c'e' traffico video attivo sul link
        self.premium_links = {
            # Link premium su S2 (percorso superiore: S2 -> S6)
            2: {
                'port': self.PORTS[2]['to_s6_premium'],   # Porta 3 di S2
                'usage_mbps': 0.0,                        # Utilizzo iniziale: 0
                'capacity_mbps': self.PREMIUM_LINK_CAPACITY_MBPS,
                'dynamic_active': False,                  # Nessuna regola dinamica
                'video_active': False                     # Nessun video attivo
            },
            # Link premium su S4 (percorso inferiore: S4 -> S6)
            4: {
                'port': self.PORTS[4]['to_s6_premium'],   # Porta 3 di S4
                'usage_mbps': 0.0,                        # Utilizzo iniziale: 0
                'capacity_mbps': self.PREMIUM_LINK_CAPACITY_MBPS,
                'dynamic_active': False,                  # Nessuna regola dinamica
                'video_active': False                     # Nessun video attivo
            }
        }

        # ============================================================== #
        #  STRUTTURE DATI PER TRACCIAMENTO DEI FLUSSI                    #
        # ============================================================== #
        # Insieme dei flussi dinamici attualmente installati, ogni elemento
        # e' una tupla (dpid, match) per poter identificare e rimuovere
        # le regole specifiche
        self.dynamic_flows = set()

        # Insieme dei flussi video attualmente attivi, ogni elemento
        # e' una tupla (dpid, match) per tracciamento
        self.video_flows = set()

        # ============================================================== #
        #  VARIABILI PER IL CALCOLO DEL THROUGHPUT                       #
        # ============================================================== #
        # Dizionario per memorizzare i byte trasmessi alla lettura precedente
        # Chiave: (dpid, port_no), Valore: tx_bytes
        self.prev_tx_bytes = {}

        # Dizionario per memorizzare il timestamp della lettura precedente
        # Chiave: (dpid, port_no), Valore: timestamp
        self.prev_timestamp = {}

        # ============================================================== #
        #  DIZIONARIO DEI DATAPATH CONNESSI                              #
        # ============================================================== #
        # Manteniamo un riferimento ai datapath di ogni switch per poter
        # inviare comandi (installare/rimuovere regole) in qualsiasi momento
        self.datapaths = {}

        # Messaggio di avvio del controller
        self.logger.info("============================================")
        self.logger.info("  Dynamic Slicing Controller avviato")
        self.logger.info("  Monitoraggio link premium ogni %d sec", self.MONITOR_INTERVAL)
        self.logger.info("  Controllo allocazione dinamica ogni %d sec", self.DYNAMIC_CHECK_INTERVAL)
        self.logger.info("============================================")

    # ================================================================== #
    #  GESTIONE EVENTI: Configurazione iniziale dello switch             #
    # ================================================================== #

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Gestore dell'evento SwitchFeatures.

        Viene invocato quando uno switch si connette al controller e completa
        l'handshake OpenFlow. In questa fase:
        1. Si installa la regola table-miss (priorita' 0) che invia i pacchetti
           non corrispondenti a nessuna regola al controller tramite packet-in.
        2. Si salva il riferimento al datapath per uso futuro.
        3. Si configurano le regole di instradamento specifiche per lo switch.

        Args:
            ev: Evento contenente il messaggio SwitchFeatures con le
                informazioni sullo switch appena connesso.
        """
        # Estrazione del datapath (rappresentazione dello switch) e del protocollo
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.logger.info("Switch S%d connesso (dpid=%s)", dpid, dpid)

        # Salvataggio del datapath per poterlo usare nei thread di monitoraggio
        self.datapaths[dpid] = datapath

        # ------------------------------------------------------------ #
        #  Installazione della regola TABLE-MISS                        #
        # ------------------------------------------------------------ #
        # La regola table-miss ha priorita' 0 e un match vuoto (corrisponde
        # a tutti i pacchetti). L'azione e' OUTPUT verso il controller
        # (OFPP_CONTROLLER) con buffer massimo di OFPCML_NO_BUFFER.
        # Questo garantisce che ogni pacchetto senza regola specifica
        # venga inviato al controller per l'analisi.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, 0, match, actions)
        self.logger.info("  -> Regola table-miss installata su S%d", dpid)

        # ------------------------------------------------------------ #
        #  Configurazione delle regole specifiche per ogni switch       #
        # ------------------------------------------------------------ #
        self._configure_switch(datapath)

        # ------------------------------------------------------------ #
        #  Avvio dei thread di monitoraggio (solo una volta)            #
        # ------------------------------------------------------------ #
        # I thread vengono avviati quando si connette il primo switch.
        # Usiamo un flag per evitare di avviarli piu' volte.
        if not hasattr(self, '_monitor_started'):
            self._monitor_started = True
            # Thread per il monitoraggio delle statistiche delle porte
            hub.spawn(self._monitor_loop)
            # Thread per il controllo dell'allocazione dinamica
            hub.spawn(self._dynamic_slicing_loop)
            self.logger.info("Thread di monitoraggio e dynamic slicing avviati")

    # ================================================================== #
    #  CONFIGURAZIONE DEGLI SWITCH: Regole di instradamento              #
    # ================================================================== #

    def _configure_switch(self, datapath):
        """
        Configura le regole di instradamento per uno specifico switch.

        Ogni switch ha regole diverse in base al suo ruolo nella topologia:
        - S1: smista il traffico tra percorso superiore (verso H3) e inferiore (verso H4)
        - S2, S4: separano traffico video (premium) e normale (standard)
        - S3, S5: inoltrano il traffico standard verso S6
        - S6: consegna il traffico ai client H3 e H4

        Le regole per il traffico video (UDP porta 9999) hanno priorita' 200
        e vengono sempre instradate sui link premium.
        Le regole per il traffico normale hanno priorita' 100 e vengono
        instradate sui link standard.

        Args:
            datapath: Il datapath dello switch da configurare.
        """
        dpid = datapath.id
        parser = datapath.ofproto_parser

        self.logger.info("Configurazione regole per S%d...", dpid)

        if dpid == 1:
            self._configure_s1(datapath, parser)
        elif dpid == 2:
            self._configure_s2(datapath, parser)
        elif dpid == 3:
            self._configure_s3(datapath, parser)
        elif dpid == 4:
            self._configure_s4(datapath, parser)
        elif dpid == 5:
            self._configure_s5(datapath, parser)
        elif dpid == 6:
            self._configure_s6(datapath, parser)

    def _configure_s1(self, datapath, parser):
        """
        Configurazione Switch 1 (S1) - Switch di ingresso.

        S1 e' il primo switch della topologia, connesso direttamente ai
        server H1 (porta 1) e H2 (porta 2). Il suo compito e' smistare
        il traffico verso il percorso superiore (porta 3 -> S2) o inferiore
        (porta 4 -> S4) in base alla destinazione.

        Regole installate:
        - Traffico verso H3 (sia da H1 che H2) -> percorso superiore (S2)
        - Traffico verso H4 (sia da H1 che H2) -> percorso inferiore (S4)
        - Traffico di ritorno da S2/S4 verso H1/H2

        Args:
            datapath: Datapath di S1.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 1
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Traffico verso H3: H1/H2 -> percorso superiore (S2)     #
        # ------------------------------------------------------------ #
        # Da H1 verso H3: entra dalla porta H1, esce verso S2
        match = parser.OFPMatch(eth_dst=self.H3_MAC)
        actions = [parser.OFPActionOutput(ports['to_upper'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S1: Traffico verso H3 -> percorso superiore (porta %d)",
                         ports['to_upper'])

        # ------------------------------------------------------------ #
        #  Traffico verso H4: H1/H2 -> percorso inferiore (S4)     #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(eth_dst=self.H4_MAC)
        actions = [parser.OFPActionOutput(ports['to_lower'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S1: Traffico verso H4 -> percorso inferiore (porta %d)",
                         ports['to_lower'])

        # ------------------------------------------------------------ #
        #  Traffico di ritorno verso H1: da S2/S4 -> porta H1      #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(eth_dst=self.H1_MAC)
        actions = [parser.OFPActionOutput(ports['H1'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S1: Traffico verso H1 -> porta %d", ports['H1'])

        # ------------------------------------------------------------ #
        #  Traffico di ritorno verso H2: da S2/S4 -> porta H2      #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(eth_dst=self.H2_MAC)
        actions = [parser.OFPActionOutput(ports['H2'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S1: Traffico verso H2 -> porta %d", ports['H2'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        # Il traffico ARP viene inviato su tutte le porte tranne quella
        # di ingresso per garantire la raggiungibilita' di tutti gli host
        self._install_arp_flooding(datapath, parser, dpid)

    def _configure_s2(self, datapath, parser):
        """
        Configurazione Switch 2 (S2) - Switch percorso superiore con link premium.

        S2 e' uno switch critico: separa il traffico video da quello normale.
        - Traffico video (UDP porta 9999) -> link premium diretto verso S6 (porta 3)
        - Traffico normale -> link standard verso S3 (porta 2)
        - Traffico di ritorno -> verso S1 (porta 1)

        Il link premium (porta 3) e' quello monitorato per il dynamic slicing.

        Args:
            datapath: Datapath di S2.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 2
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  REGOLA VIDEO: traffico UDP 9999 -> link premium (priorita' alta) #
        # ------------------------------------------------------------ #
        # Il traffico video viene SEMPRE instradato sul link premium
        # indipendentemente da qualsiasi altra condizione.
        # Match: pacchetto IPv4, protocollo UDP, porta destinazione 9999
        match = parser.OFPMatch(
            eth_type=self.ETH_TYPE_IP,
            ip_proto=self.IP_PROTO_UDP,
            udp_dst=self.VIDEO_UDP_PORT
        )
        actions = [parser.OFPActionOutput(ports['to_s6_premium'])]
        self._add_flow(datapath, self.PRIORITY_VIDEO, match, actions)
        self.logger.info("  S2: Traffico VIDEO (UDP %d) -> link premium (porta %d)",
                         self.VIDEO_UDP_PORT, ports['to_s6_premium'])

        # ------------------------------------------------------------ #
        #  REGOLA STANDARD: traffico normale -> link standard            #
        # ------------------------------------------------------------ #
        # Tutto il traffico IP non video proveniente da S1 viene instradato
        # sul percorso standard attraverso S3
        match = parser.OFPMatch(
            in_port=ports['to_s1'],
            eth_type=self.ETH_TYPE_IP
        )
        actions = [parser.OFPActionOutput(ports['to_s3_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S2: Traffico normale -> link standard (porta %d)",
                         ports['to_s3_standard'])

        # ------------------------------------------------------------ #
        #  REGOLA RITORNO: traffico da S3/S6 -> verso S1                #
        # ------------------------------------------------------------ #
        # Il traffico di ritorno (da S6 premium o da S3 standard) viene
        # inoltrato verso S1
        match = parser.OFPMatch(in_port=ports['to_s3_standard'])
        actions = [parser.OFPActionOutput(ports['to_s1'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)

        match = parser.OFPMatch(in_port=ports['to_s6_premium'])
        actions = [parser.OFPActionOutput(ports['to_s1'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S2: Traffico di ritorno -> S1 (porta %d)", ports['to_s1'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        self._install_arp_flooding(datapath, parser, dpid)

    def _configure_s3(self, datapath, parser):
        """
        Configurazione Switch 3 (S3) - Switch intermedio percorso standard superiore.

        S3 si trova sul percorso standard superiore tra S2 e S6.
        Inoltra semplicemente il traffico in entrambe le direzioni:
        - Da S2 (porta 1) verso S6 (porta 2)
        - Da S6 (porta 2) verso S2 (porta 1)

        Args:
            datapath: Datapath di S3.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 3
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Inoltro bidirezionale tra S2 e S6                            #
        # ------------------------------------------------------------ #
        # Direzione S2 -> S6 (traffico verso i client)
        match = parser.OFPMatch(in_port=ports['to_s2'])
        actions = [parser.OFPActionOutput(ports['to_s6'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S3: Da S2 (porta %d) -> S6 (porta %d)",
                         ports['to_s2'], ports['to_s6'])

        # Direzione S6 -> S2 (traffico di ritorno)
        match = parser.OFPMatch(in_port=ports['to_s6'])
        actions = [parser.OFPActionOutput(ports['to_s2'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S3: Da S6 (porta %d) -> S2 (porta %d)",
                         ports['to_s6'], ports['to_s2'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        self._install_arp_flooding(datapath, parser, dpid)

    def _configure_s4(self, datapath, parser):
        """
        Configurazione Switch 4 (S4) - Switch percorso inferiore con link premium.

        S4 ha lo stesso ruolo di S2 ma per il percorso inferiore.
        Separa il traffico video da quello normale:
        - Traffico video (UDP porta 9999) -> link premium diretto verso S6 (porta 3)
        - Traffico normale -> link standard verso S5 (porta 2)
        - Traffico di ritorno -> verso S1 (porta 1)

        Il link premium (porta 3) e' monitorato per il dynamic slicing.

        Args:
            datapath: Datapath di S4.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 4
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  REGOLA VIDEO: traffico UDP 9999 -> link premium (priorita' alta) #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(
            eth_type=self.ETH_TYPE_IP,
            ip_proto=self.IP_PROTO_UDP,
            udp_dst=self.VIDEO_UDP_PORT
        )
        actions = [parser.OFPActionOutput(ports['to_s6_premium'])]
        self._add_flow(datapath, self.PRIORITY_VIDEO, match, actions)
        self.logger.info("  S4: Traffico VIDEO (UDP %d) -> link premium (porta %d)",
                         self.VIDEO_UDP_PORT, ports['to_s6_premium'])

        # ------------------------------------------------------------ #
        #  REGOLA STANDARD: traffico normale -> link standard            #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(
            in_port=ports['to_s1'],
            eth_type=self.ETH_TYPE_IP
        )
        actions = [parser.OFPActionOutput(ports['to_s5_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S4: Traffico normale -> link standard (porta %d)",
                         ports['to_s5_standard'])

        # ------------------------------------------------------------ #
        #  REGOLA RITORNO: traffico da S5/S6 -> verso S1                #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(in_port=ports['to_s5_standard'])
        actions = [parser.OFPActionOutput(ports['to_s1'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)

        match = parser.OFPMatch(in_port=ports['to_s6_premium'])
        actions = [parser.OFPActionOutput(ports['to_s1'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S4: Traffico di ritorno -> S1 (porta %d)", ports['to_s1'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        self._install_arp_flooding(datapath, parser, dpid)

    def _configure_s5(self, datapath, parser):
        """
        Configurazione Switch 5 (S5) - Switch intermedio percorso standard inferiore.

        S5 si trova sul percorso standard inferiore tra S4 e S6.
        Inoltra semplicemente il traffico in entrambe le direzioni:
        - Da S4 (porta 1) verso S6 (porta 2)
        - Da S6 (porta 2) verso S4 (porta 1)

        Args:
            datapath: Datapath di S5.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 5
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Inoltro bidirezionale tra S4 e S6                            #
        # ------------------------------------------------------------ #
        # Direzione S4 -> S6 (traffico verso i client)
        match = parser.OFPMatch(in_port=ports['to_s4'])
        actions = [parser.OFPActionOutput(ports['to_s6'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S5: Da S4 (porta %d) -> S6 (porta %d)",
                         ports['to_s4'], ports['to_s6'])

        # Direzione S6 -> S4 (traffico di ritorno)
        match = parser.OFPMatch(in_port=ports['to_s6'])
        actions = [parser.OFPActionOutput(ports['to_s4'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S5: Da S6 (porta %d) -> S4 (porta %d)",
                         ports['to_s6'], ports['to_s4'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        self._install_arp_flooding(datapath, parser, dpid)

    def _configure_s6(self, datapath, parser):
        """
        Configurazione Switch 6 (S6) - Switch di uscita.

        S6 e' l'ultimo switch della topologia, connesso direttamente ai
        client H3 (porta 3) e H4 (porta 6). Riceve traffico da quattro
        direzioni diverse:
        - Porta 1: dal percorso standard superiore (da S3)
        - Porta 2: dal link premium superiore (da S2)
        - Porta 4: dal percorso standard inferiore (da S5)
        - Porta 5: dal link premium inferiore (da S4)

        Tutto il traffico destinato a H3 viene inoltrato sulla porta 3,
        tutto il traffico destinato a H4 viene inoltrato sulla porta 6.

        Args:
            datapath: Datapath di S6.
            parser: Parser OpenFlow per la creazione di match e azioni.
        """
        dpid = 6
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Consegna traffico a H3 (porta 3)                            #
        # ------------------------------------------------------------ #
        # Qualsiasi traffico con destinazione MAC di H3 viene inoltrato
        # sulla porta 3, indipendentemente dalla porta di ingresso
        match = parser.OFPMatch(eth_dst=self.H3_MAC)
        actions = [parser.OFPActionOutput(ports['H3'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S6: Traffico verso H3 -> porta %d", ports['H3'])

        # ------------------------------------------------------------ #
        #  Consegna traffico a H4 (porta 6)                            #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(eth_dst=self.H4_MAC)
        actions = [parser.OFPActionOutput(ports['H4'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S6: Traffico verso H4 -> porta %d", ports['H4'])

        # ------------------------------------------------------------ #
        #  Traffico di ritorno: da H3 -> percorso superiore (S2/S3)    #
        # ------------------------------------------------------------ #
        # Il traffico di ritorno da H3 verso H1 va sul percorso superiore
        # Usiamo il link standard (verso S3) per il ritorno
        match = parser.OFPMatch(
            in_port=ports['H3'],
            eth_dst=self.H1_MAC
        )
        actions = [parser.OFPActionOutput(ports['from_s3_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)

        match = parser.OFPMatch(
            in_port=ports['H3'],
            eth_dst=self.H2_MAC
        )
        actions = [parser.OFPActionOutput(ports['from_s3_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S6: Ritorno da H3 -> S3 standard (porta %d)",
                         ports['from_s3_standard'])

        # ------------------------------------------------------------ #
        #  Traffico di ritorno: da H4 -> percorso inferiore (S4/S5)    #
        # ------------------------------------------------------------ #
        match = parser.OFPMatch(
            in_port=ports['H4'],
            eth_dst=self.H1_MAC
        )
        actions = [parser.OFPActionOutput(ports['from_s5_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)

        match = parser.OFPMatch(
            in_port=ports['H4'],
            eth_dst=self.H2_MAC
        )
        actions = [parser.OFPActionOutput(ports['from_s5_standard'])]
        self._add_flow(datapath, self.PRIORITY_DEFAULT, match, actions)
        self.logger.info("  S6: Ritorno da H4 -> S5 standard (porta %d)",
                         ports['from_s5_standard'])

        # ------------------------------------------------------------ #
        #  Regole ARP: flooding per la risoluzione degli indirizzi      #
        # ------------------------------------------------------------ #
        self._install_arp_flooding(datapath, parser, dpid)

    # ================================================================== #
    #  REGOLE ARP: Flooding per la risoluzione degli indirizzi           #
    # ================================================================== #

    def _install_arp_flooding(self, datapath, parser, dpid):
        """
        Installa regole di forwarding ARP specifiche per ogni switch.

        NOTA IMPORTANTE: Non si puo fare semplice flooding su questa topologia
        perche i Premium Links (S2:3-S6:2 e S4:3-S6:5) creano cicli.
        Un broadcast ARP in flood causerebbe una tempesta di pacchetti.

        La soluzione e installare regole ARP per ogni porta di ingresso
        che inoltrano il broadcast solo sulle porte corrette, usando
        ESCLUSIVAMENTE i percorsi standard (escludendo i premium links).

        Porte premium da escludere:
        - S2 porta 3 (to_s6_premium)
        - S4 porta 3 (to_s6_premium)
        - S6 porta 2 (from_s2_premium)
        - S6 porta 5 (from_s4_premium)

        Args:
            datapath: Datapath dello switch.
            parser: Parser OpenFlow.
            dpid: ID dello switch.
        """
        # Mappa di forwarding ARP specifico per switch.
        # Per ogni switch e porta di ingresso, definisce le porte di uscita.
        # Le porte premium sono ESCLUSE per evitare loop.
        arp_forward = {
            1: {1: [3, 4], 2: [3, 4], 3: [1, 2, 4], 4: [1, 2, 3]},
            2: {1: [2], 2: [1]},       # porta 3 (premium) esclusa
            3: {1: [2], 2: [1]},
            4: {1: [2], 2: [1]},       # porta 3 (premium) esclusa
            5: {1: [2], 2: [1]},
            6: {1: [3, 4, 6], 3: [1, 4, 6], 4: [1, 3, 6], 6: [1, 3, 4]},
            # S6: porte 2 e 5 (premium) escluse sia come ingresso che uscita
        }

        if dpid in arp_forward:
            for in_port, out_ports in arp_forward[dpid].items():
                actions = [parser.OFPActionOutput(p) for p in out_ports]
                match = parser.OFPMatch(in_port=in_port, eth_type=self.ETH_TYPE_ARP)
                self._add_flow(datapath, self.PRIORITY_ARP, match, actions)

        self.logger.info("  S%d: Regole ARP installate (no premium, no loop)", dpid)

    # ================================================================== #
    #  UTILITY: Registrazione eventi per la dashboard                    #
    # ================================================================== #

    def _add_event(self, event_type, message):
        """
        Aggiunge un evento al buffer circolare per il registro della dashboard.

        Args:
            event_type: Tipo di evento ('video', 'dynamic', 'preemption').
            message: Messaggio descrittivo dell'evento.
        """
        event = {
            'timestamp': time.strftime('%H:%M:%S'),
            'type': event_type,
            'message': message
        }
        with self.stats_lock:
            self.dashboard_events.insert(0, event)
            if len(self.dashboard_events) > self.MAX_EVENTS:
                self.dashboard_events = self.dashboard_events[:self.MAX_EVENTS]

    # ================================================================== #
    #  UTILITY: Installazione e rimozione di regole OpenFlow             #
    # ================================================================== #

    def _add_flow(self, datapath, priority, match, actions, idle_timeout=0,
                  hard_timeout=0):
        """
        Installa una regola OpenFlow su uno switch.

        Crea un messaggio OFPFlowMod con i parametri specificati e lo invia
        allo switch. La regola viene aggiunta alla tabella dei flussi dello
        switch con la priorita' e i timeout indicati.

        Args:
            datapath: Datapath dello switch su cui installare la regola.
            priority: Priorita' della regola (numero piu' alto = priorita' maggiore).
            match: Condizioni di match per la regola (quali pacchetti corrispondono).
            actions: Lista di azioni da eseguire sui pacchetti corrispondenti.
            idle_timeout: Tempo in secondi dopo il quale la regola viene rimossa
                          se non ci sono pacchetti corrispondenti (0 = mai).
            hard_timeout: Tempo in secondi dopo il quale la regola viene rimossa
                          indipendentemente dall'uso (0 = mai).
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Creazione dell'istruzione "applica azioni" che contiene la lista
        # delle azioni da eseguire sui pacchetti corrispondenti
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        # Creazione e invio del messaggio FlowMod per installare la regola
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        datapath.send_msg(mod)

    def _delete_flow(self, datapath, match, priority):
        """
        Rimuove una regola OpenFlow specifica da uno switch.

        Utilizza il comando OFPFC_DELETE per rimuovere tutte le regole che
        corrispondono al match e alla priorita' specificati. I parametri
        out_port=OFPP_ANY e out_group=OFPG_ANY assicurano che la regola
        venga rimossa indipendentemente dalla porta o dal gruppo di uscita.

        Args:
            datapath: Datapath dello switch da cui rimuovere la regola.
            match: Condizioni di match della regola da rimuovere.
            priority: Priorita' della regola da rimuovere.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Creazione del messaggio FlowMod con comando DELETE
        # OFPP_ANY e OFPG_ANY indicano di non filtrare per porta/gruppo di uscita
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match,
            priority=priority
        )
        datapath.send_msg(mod)
        self.logger.info("  Regola rimossa su S%d (priorita' %d)",
                         datapath.id, priority)

    # ================================================================== #
    #  THREAD DI MONITORAGGIO: Richiesta statistiche porte               #
    # ================================================================== #

    def _monitor_loop(self):
        """
        Thread di monitoraggio delle statistiche delle porte.

        Questo thread viene eseguito in background e, ad intervalli regolari
        (MONITOR_INTERVAL secondi), invia richieste di statistiche delle porte
        agli switch S2 e S4 (quelli con link premium).

        Le risposte vengono gestite dal metodo _port_stats_reply_handler
        che calcola il throughput effettivo sui link premium.

        Il thread utilizza hub.sleep() per le pause, che e' la versione
        cooperativa di time.sleep() compatibile con l'event loop di Ryu.
        """
        self.logger.info("Monitor loop avviato - intervallo: %d secondi",
                         self.MONITOR_INTERVAL)

        while True:
            # Pausa prima della prossima richiesta di statistiche
            hub.sleep(self.MONITOR_INTERVAL)

            # Invio richiesta di statistiche per TUTTI gli switch connessi
            for dpid in list(self.datapaths.keys()):
                if dpid in self.datapaths:
                    datapath = self.datapaths[dpid]
                    parser = datapath.ofproto_parser
                    # Richiesta delle statistiche di TUTTE le porte dello switch
                    req = parser.OFPPortStatsRequest(datapath, 0,
                                                     datapath.ofproto.OFPP_ANY)
                    datapath.send_msg(req)
                    self.logger.debug("Richiesta port stats inviata a S%d", dpid)

    # ================================================================== #
    #  GESTIONE EVENTI: Risposta statistiche porte                       #
    # ================================================================== #

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
        Gestore della risposta alle richieste di statistiche delle porte.

        Quando uno switch risponde con le statistiche delle porte, questo
        metodo calcola il throughput effettivo sul link premium confrontando
        i byte trasmessi (tx_bytes) con la lettura precedente.

        Il calcolo del throughput utilizza:
        1. Delta dei byte trasmessi tra due letture consecutive
        2. Delta temporale tra le due letture
        3. Conversione da byte/secondo a Megabit/secondo
        4. Smoothing esponenziale per evitare oscillazioni:
           nuovo_valore = 0.7 * vecchio_valore + 0.3 * valore_misurato

        Lo smoothing e' importante perche' le misure istantanee possono
        essere molto variabili. Il fattore 0.7/0.3 bilancia tra stabilita'
        (reagire lentamente ai cambiamenti) e reattivita' (adattarsi
        rapidamente alle nuove condizioni).

        Args:
            ev: Evento contenente la risposta con le statistiche delle porte.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        # Timestamp corrente per il calcolo del delta temporale
        current_time = time.time()

        # Porta del link premium per questo switch (se ha un link premium)
        premium_port = None
        if dpid in self.premium_links:
            premium_port = self.premium_links[dpid]['port']

        for stat in body:
            # Ignora porte speciali (LOCAL, etc.)
            if stat.port_no > 65000:
                continue

            # Chiave univoca per identificare questa combinazione switch/porta
            key = (dpid, stat.port_no)

            # Salvataggio statistiche grezze per tutte le porte
            with self.stats_lock:
                if dpid not in self.port_stats:
                    self.port_stats[dpid] = {}
                self.port_stats[dpid][stat.port_no] = {
                    'tx_bytes': stat.tx_bytes,
                    'rx_bytes': stat.rx_bytes,
                    'tx_packets': stat.tx_packets,
                    'rx_packets': stat.rx_packets,
                    'tx_errors': stat.tx_errors,
                    'rx_errors': stat.rx_errors,
                }

            # Calcolo della velocita' (tx_bps) da delta bytes / delta time
            tx_bps = 0.0
            have_prev = key in self.prev_tx_bytes
            if have_prev:
                delta_bytes = stat.tx_bytes - self.prev_tx_bytes[key]
                delta_time = current_time - self.prev_timestamp[key]

                if delta_time > 0 and delta_bytes >= 0:
                    tx_bps = (delta_bytes * 8.0) / delta_time

            # Salvataggio dei valori correnti per la prossima iterazione
            self.prev_tx_bytes[key] = stat.tx_bytes
            self.prev_timestamp[key] = current_time

            # Aggiornamento port_speed per tutte le porte (dopo il primo ciclo)
            if have_prev:
                with self.stats_lock:
                    if dpid not in self.port_speed:
                        self.port_speed[dpid] = {}
                    self.port_speed[dpid][stat.port_no] = {
                        'tx_bps': tx_bps,
                        'tx_mbps': tx_bps / 1000000.0
                    }

            # Calcolo throughput smoothed per link premium
            # Aggiorna SEMPRE (anche con tx_bps=0) cosi' il valore decade
            if premium_port is not None and stat.port_no == premium_port and have_prev:
                throughput_mbps = tx_bps / 1000000.0
                old_usage = self.premium_links[dpid]['usage_mbps']
                smoothed_usage = 0.7 * old_usage + 0.3 * throughput_mbps
                self.premium_links[dpid]['usage_mbps'] = smoothed_usage

                self.logger.info(
                    "S%d porta premium %d: throughput=%.3f Mbps, "
                    "smoothed=%.3f Mbps (%.1f%% capacita')",
                    dpid, premium_port, throughput_mbps, smoothed_usage,
                    (smoothed_usage / self.premium_links[dpid]['capacity_mbps']) * 100
                )

        # Scrittura metriche su InfluxDB (per Grafana)
        if self.influx is not None:
            try:
                points = []
                for d, info in self.premium_links.items():
                    points.append({
                        "measurement": "premium_link",
                        "tags": {"switch": "s%d" % d,
                                 "path": "upper" if d == 2 else "lower"},
                        "fields": {
                            "usage_mbps": float(info['usage_mbps']),
                            "capacity_mbps": float(info['capacity_mbps']),
                            "usage_pct": float(info['usage_mbps'] /
                                               info['capacity_mbps'] * 100),
                            "video_active": int(info['video_active']),
                            "dynamic_active": int(info['dynamic_active']),
                            "preemption_count": self.preemption_count,
                        }
                    })
                # Aggiungi anche le velocita' per-porta
                for d, ports in self.port_speed.items():
                    for port_no, speed in ports.items():
                        points.append({
                            "measurement": "port_throughput",
                            "tags": {"switch": "s%d" % d,
                                     "port": str(port_no)},
                            "fields": {
                                "tx_mbps": float(speed.get('tx_mbps', 0)),
                            }
                        })
                self.influx.write_points(points)
            except Exception:
                pass  # non bloccare il controller se InfluxDB non risponde

    # ================================================================== #
    #  THREAD DI DYNAMIC SLICING: Allocazione e revoca dinamica          #
    # ================================================================== #

    def _dynamic_slicing_loop(self):
        """
        Thread di controllo per l'allocazione dinamica della banda.

        Questo thread viene eseguito in background e, ad intervalli regolari
        (DYNAMIC_CHECK_INTERVAL secondi), valuta per ogni link premium se:

        1. ALLOCARE traffico dinamico: se il link premium e' sottoutilizzato
           (utilizzo < 30% della capacita') e non c'e' traffico video attivo,
           installa regole a bassa priorita' per instradare il traffico UDP
           porta 800 sul link premium.

        2. REVOCARE traffico dinamico (PREEMPTION): se il link premium e'
           sovraccarico (utilizzo >= 80%) oppure e' arrivato traffico video,
           rimuove immediatamente tutte le regole dinamiche per liberare
           banda sul link premium.

        Le soglie sono:
        - BANDWIDTH_THRESHOLD (30%): sotto questa soglia si puo' allocare
        - PREEMPTION_THRESHOLD (80%): sopra questa soglia si deve revocare

        Il gap tra le due soglie (30%-80%) serve come zona di isteresi per
        evitare oscillazioni continue tra allocazione e revoca.
        """
        self.logger.info("Dynamic slicing loop avviato - intervallo: %d secondi",
                         self.DYNAMIC_CHECK_INTERVAL)

        while True:
            # Pausa prima del prossimo controllo
            hub.sleep(self.DYNAMIC_CHECK_INTERVAL)

            # Controllo per ogni link premium (S2 e S4)
            for dpid, link_info in self.premium_links.items():
                # Verifica che lo switch sia connesso
                if dpid not in self.datapaths:
                    continue

                datapath = self.datapaths[dpid]
                parser = datapath.ofproto_parser

                # Calcolo della percentuale di utilizzo del link premium
                usage_ratio = link_info['usage_mbps'] / link_info['capacity_mbps']

                # ---------------------------------------------------- #
                #  RILEVAMENTO VIDEO basato su throughput               #
                # ---------------------------------------------------- #
                # Le regole video statiche (priorita' 200) gestiscono i
                # pacchetti direttamente sullo switch senza packet-in,
                # quindi rileviamo il video dal throughput sul premium link.
                # Solo il traffico video (UDP 9999) e' instradato
                # staticamente sul premium link; il traffico dinamico
                # (UDP 800) e' diverso e ha priorita' inferiore.
                if not link_info['video_active'] and usage_ratio > 0.25:
                    link_info['video_active'] = True
                    path_name = 'superiore (S2 -> S6)' if dpid == 2 else 'inferiore (S4 -> S6)'
                    self._add_event('video',
                                    'Flusso video rilevato sul percorso %s (throughput: %.1f Mbps)'
                                    % (path_name, link_info['usage_mbps']))
                    self.logger.info(
                        "Video rilevato su S%d tramite throughput: %.1f Mbps",
                        dpid, link_info['usage_mbps']
                    )

                # ---------------------------------------------------- #
                #  RESET VIDEO: se video attivo ma utilizzo < 5%       #
                # ---------------------------------------------------- #
                if link_info['video_active'] and usage_ratio < 0.05:
                    link_info['video_active'] = False
                    self.logger.info(
                        "Video su S%d resettato: utilizzo %.1f%% (sotto 5%%)",
                        dpid, usage_ratio * 100
                    )
                    self._add_event('video',
                                    'Flusso video terminato su S%d (utilizzo sotto 5%%)' % dpid)

                # ---------------------------------------------------- #
                #  CASO 1: PREEMPTION - Rimuovere traffico dinamico    #
                # ---------------------------------------------------- #
                # La preemption scatta se:
                # - L'utilizzo supera la soglia di preemption (80%), OPPURE
                # - C'e' traffico video attivo sul link premium
                # In entrambi i casi, il traffico dinamico deve essere rimosso
                # per garantire la qualita' del servizio al traffico video
                if link_info['dynamic_active'] and (
                    usage_ratio >= self.PREEMPTION_THRESHOLD or
                    link_info['video_active']
                ):
                    self.logger.info(
                        "*** PREEMPTION su S%d: utilizzo=%.1f%%, video=%s ***",
                        dpid, usage_ratio * 100, link_info['video_active']
                    )
                    self._remove_dynamic_flows(dpid)

                # ---------------------------------------------------- #
                #  CASO 2: ALLOCAZIONE - Aggiungere traffico dinamico  #
                # ---------------------------------------------------- #
                # L'allocazione dinamica avviene se:
                # - Non ci sono gia' regole dinamiche attive
                # - L'utilizzo e' sotto la soglia di allocazione (30%)
                # - Non c'e' traffico video attivo
                elif (not link_info['dynamic_active'] and
                      usage_ratio < self.BANDWIDTH_THRESHOLD and
                      not link_info['video_active']):
                    self.logger.info(
                        ">>> ALLOCAZIONE DINAMICA su S%d: utilizzo=%.1f%% "
                        "(sotto soglia %.0f%%) <<<",
                        dpid, usage_ratio * 100, self.BANDWIDTH_THRESHOLD * 100
                    )
                    self._install_dynamic_flows(dpid)

    # ================================================================== #
    #  DYNAMIC SLICING: Installazione regole dinamiche                   #
    # ================================================================== #

    def _install_dynamic_flows(self, dpid):
        """
        Installa le regole di allocazione dinamica su un link premium.

        Quando un link premium e' sottoutilizzato, questa funzione installa
        regole OpenFlow che instradano il traffico UDP porta 800 attraverso
        il link premium invece del percorso standard.

        Le regole dinamiche hanno:
        - Priorita' PRIORITY_DYNAMIC (110): superiore a PRIORITY_DEFAULT (100),
          cosi' il traffico UDP 800 viene diretto sul premium link quando la regola
          e' attiva. Alla rimozione, il traffico ricade sulla regola default (100).
          La regola video (priorita' 200) ha sempre la precedenza.
        - idle_timeout di 300 secondi: la regola viene rimossa automaticamente
          se non viene utilizzata per 5 minuti, liberando risorse.

        Le regole vengono installate sia sullo switch con link premium (S2/S4)
        sia sullo switch di uscita (S6) per garantire il corretto instradamento
        end-to-end del traffico dinamico.

        Args:
            dpid: ID dello switch (2 o 4) su cui installare le regole dinamiche.
        """
        if dpid not in self.datapaths:
            self.logger.warning("S%d non connesso, impossibile installare regole dinamiche", dpid)
            return

        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Regola sullo switch con link premium (S2 o S4)              #
        # ------------------------------------------------------------ #
        # Match: pacchetto IPv4, protocollo UDP, porta destinazione 800,
        # proveniente dalla porta verso S1
        match = parser.OFPMatch(
            in_port=ports['to_s1'],
            eth_type=self.ETH_TYPE_IP,
            ip_proto=self.IP_PROTO_UDP,
            udp_dst=self.DYNAMIC_UDP_PORT
        )

        # Azione: inoltra sul link premium invece che sul percorso standard
        if dpid == 2:
            out_port = ports['to_s6_premium']
        elif dpid == 4:
            out_port = ports['to_s6_premium']
        else:
            return

        actions = [parser.OFPActionOutput(out_port)]
        self._add_flow(datapath, self.PRIORITY_DYNAMIC, match, actions,
                       idle_timeout=300)
        self.logger.info("  Regola dinamica installata su S%d: UDP %d -> porta %d "
                         "(premium, idle_timeout=300s)", dpid, self.DYNAMIC_UDP_PORT,
                         out_port)

        # Salvataggio del flusso dinamico per poterlo rimuovere in seguito
        self.dynamic_flows.add((dpid, self.DYNAMIC_UDP_PORT))

        # ------------------------------------------------------------ #
        #  Regola su S6 per il traffico proveniente dal link premium   #
        # ------------------------------------------------------------ #
        # S6 deve sapere come gestire il traffico dinamico che arriva
        # dal link premium (e non dal percorso standard)
        if 6 in self.datapaths:
            dp_s6 = self.datapaths[6]
            parser_s6 = dp_s6.ofproto_parser
            ports_s6 = self.PORTS[6]

            # Determina la porta di ingresso su S6 in base allo switch sorgente
            if dpid == 2:
                # Traffico dal link premium superiore (S2 -> S6)
                s6_in_port = ports_s6['from_s2_premium']
                # Destinazione: H3 (percorso superiore)
                s6_out_port = ports_s6['H3']
            elif dpid == 4:
                # Traffico dal link premium inferiore (S4 -> S6)
                s6_in_port = ports_s6['from_s4_premium']
                # Destinazione: H4 (percorso inferiore)
                s6_out_port = ports_s6['H4']
            else:
                return

            # Match su S6: traffico UDP 800 proveniente dal link premium
            match_s6 = parser_s6.OFPMatch(
                in_port=s6_in_port,
                eth_type=self.ETH_TYPE_IP,
                ip_proto=self.IP_PROTO_UDP,
                udp_dst=self.DYNAMIC_UDP_PORT
            )
            actions_s6 = [parser_s6.OFPActionOutput(s6_out_port)]
            self._add_flow(dp_s6, self.PRIORITY_DYNAMIC, match_s6, actions_s6,
                           idle_timeout=300)
            self.logger.info("  Regola dinamica installata su S6: da porta %d -> "
                             "porta %d (idle_timeout=300s)", s6_in_port, s6_out_port)

            # Salvataggio del flusso dinamico su S6
            self.dynamic_flows.add((6, self.DYNAMIC_UDP_PORT, dpid))

        # Aggiornamento dello stato del link premium
        self.premium_links[dpid]['dynamic_active'] = True
        path_name = 'superiore (S2 -> S6)' if dpid == 2 else 'inferiore (S4 -> S6)'
        self._add_event('dynamic',
                        'Allocazione dinamica attivata sul percorso %s' % path_name)
        self.logger.info("  Allocazione dinamica COMPLETATA su S%d", dpid)

    # ================================================================== #
    #  DYNAMIC SLICING: Rimozione regole dinamiche (PREEMPTION)          #
    # ================================================================== #

    def _remove_dynamic_flows(self, dpid):
        """
        Rimuove tutte le regole di allocazione dinamica da un link premium.

        Questa funzione implementa il meccanismo di PREEMPTION: quando il
        traffico video arriva o il link premium diventa troppo utilizzato,
        tutte le regole dinamiche vengono immediatamente rimosse.

        La rimozione avviene sia sullo switch con link premium (S2/S4) che
        sullo switch di uscita (S6), garantendo che il traffico dinamico
        venga reindirizzato sul percorso standard.

        Dopo la rimozione, il traffico UDP porta 800 tornera' a seguire
        la regola standard (priorita' 100) che lo instrada sul percorso
        standard attraverso S3 (per S2) o S5 (per S4).

        Args:
            dpid: ID dello switch (2 o 4) da cui rimuovere le regole dinamiche.
        """
        if dpid not in self.datapaths:
            self.logger.warning("S%d non connesso, impossibile rimuovere regole dinamiche", dpid)
            return

        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser
        ports = self.PORTS[dpid]

        # ------------------------------------------------------------ #
        #  Rimozione regola dallo switch con link premium (S2/S4)      #
        # ------------------------------------------------------------ #
        # Creiamo lo stesso match usato durante l'installazione per
        # identificare la regola da rimuovere
        match = parser.OFPMatch(
            in_port=ports['to_s1'],
            eth_type=self.ETH_TYPE_IP,
            ip_proto=self.IP_PROTO_UDP,
            udp_dst=self.DYNAMIC_UDP_PORT
        )
        self._delete_flow(datapath, match, self.PRIORITY_DYNAMIC)
        self.logger.info("  Regola dinamica RIMOSSA da S%d", dpid)

        # ------------------------------------------------------------ #
        #  Rimozione regola corrispondente su S6                       #
        # ------------------------------------------------------------ #
        if 6 in self.datapaths:
            dp_s6 = self.datapaths[6]
            parser_s6 = dp_s6.ofproto_parser
            ports_s6 = self.PORTS[6]

            # Determinazione della porta di ingresso su S6
            if dpid == 2:
                s6_in_port = ports_s6['from_s2_premium']
            elif dpid == 4:
                s6_in_port = ports_s6['from_s4_premium']
            else:
                return

            # Match su S6 per la regola dinamica da rimuovere
            match_s6 = parser_s6.OFPMatch(
                in_port=s6_in_port,
                eth_type=self.ETH_TYPE_IP,
                ip_proto=self.IP_PROTO_UDP,
                udp_dst=self.DYNAMIC_UDP_PORT
            )
            self._delete_flow(dp_s6, match_s6, self.PRIORITY_DYNAMIC)
            self.logger.info("  Regola dinamica RIMOSSA da S6 (porta ingresso %d)",
                             s6_in_port)

        # ------------------------------------------------------------ #
        #  Pulizia delle strutture dati di tracciamento                #
        # ------------------------------------------------------------ #
        # Rimozione dei flussi dinamici dall'insieme di tracciamento
        flows_to_remove = set()
        for flow in self.dynamic_flows:
            # I flussi possono essere (dpid, port) o (dpid, port, source_dpid)
            if flow[0] == dpid or (len(flow) > 2 and flow[2] == dpid):
                flows_to_remove.add(flow)
        self.dynamic_flows -= flows_to_remove

        # Aggiornamento dello stato del link premium
        self.premium_links[dpid]['dynamic_active'] = False
        self.preemption_count += 1
        path_name = 'superiore (S2 -> S6)' if dpid == 2 else 'inferiore (S4 -> S6)'
        self._add_event('preemption',
                        'Preemption eseguita sul percorso %s - traffico dinamico rimosso' % path_name)
        self.logger.info("  Preemption COMPLETATA su S%d - traffico dinamico rimosso",
                         dpid)

    # ================================================================== #
    #  GESTIONE EVENTI: Packet-In (rilevamento traffico video)           #
    # ================================================================== #

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
        Gestore degli eventi Packet-In.

        Questo metodo viene invocato ogni volta che uno switch invia un
        pacchetto al controller (tramite la regola table-miss o altre regole
        con azione OUTPUT verso il controller).

        Il suo compito principale e' il RILEVAMENTO DEL TRAFFICO VIDEO:
        quando viene rilevato un pacchetto UDP con porta destinazione 9999,
        il controller:
        1. Marca il link premium corrispondente come "video_active"
        2. Forza immediatamente la preemption del traffico dinamico
        3. Aggiunge il flusso video all'insieme di tracciamento

        Questo garantisce che il traffico video abbia sempre la massima
        priorita' sui link premium, anche se ci sono regole dinamiche
        gia' installate.

        Per il traffico non-video, il pacchetto viene semplicemente
        loggato per scopi diagnostici.

        Args:
            ev: Evento contenente il messaggio Packet-In con il pacchetto
                ricevuto dallo switch.
        """
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Estrazione del numero di porta di ingresso dal match del pacchetto
        in_port = msg.match['in_port']

        # ------------------------------------------------------------ #
        #  Parsing del pacchetto ricevuto                              #
        # ------------------------------------------------------------ #
        # Utilizziamo la libreria ryu.lib.packet per analizzare il contenuto
        # del pacchetto e estrarre le informazioni dei vari livelli
        pkt = packet.Packet(msg.data)

        # Livello 2: Ethernet (sempre presente)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            # Pacchetto non Ethernet: non dovrebbe accadere, ma gestiamo il caso
            return

        # Livello 3: IPv4 (presente solo per traffico IP)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        # Livello 4: UDP (presente solo per traffico UDP)
        udp_pkt = pkt.get_protocol(udp.udp)

        # ------------------------------------------------------------ #
        #  RILEVAMENTO TRAFFICO VIDEO                                  #
        # ------------------------------------------------------------ #
        # Controlliamo se il pacchetto e' un segmento UDP con porta
        # destinazione 9999 (traffico video)
        if udp_pkt is not None and udp_pkt.dst_port == self.VIDEO_UDP_PORT:
            self.logger.info(
                "========================================="
            )
            self.logger.info(
                "  TRAFFICO VIDEO RILEVATO su S%d porta %d!", dpid, in_port
            )
            self.logger.info(
                "  Sorgente: %s -> Destinazione: %s",
                eth.src, eth.dst
            )
            if ip_pkt is not None:
                self.logger.info(
                    "  IP: %s -> %s, UDP porta: %d",
                    ip_pkt.src, ip_pkt.dst, udp_pkt.dst_port
                )
            self.logger.info(
                "========================================="
            )

            # Tracciamento del flusso video
            self.video_flows.add((dpid, in_port, self.VIDEO_UDP_PORT))

            # ---------------------------------------------------- #
            #  Marcatura del link premium come "video_active"      #
            # ---------------------------------------------------- #
            # Determiniamo quale link premium e' coinvolto in base
            # allo switch e alla porta di ingresso
            #
            # Se il pacchetto video arriva su S2 o S4, il link premium
            # corrispondente deve essere marcato come attivo
            if dpid in self.premium_links:
                self.premium_links[dpid]['video_active'] = True
                path_name = 'superiore (S2 -> S6)' if dpid == 2 else 'inferiore (S4 -> S6)'
                self._add_event('video',
                                'Flusso video rilevato sul percorso %s' % path_name)
                self.logger.info(
                    "Link premium S%d marcato come VIDEO ATTIVO", dpid
                )

                # ------------------------------------------------ #
                #  PREEMPTION IMMEDIATA del traffico dinamico      #
                # ------------------------------------------------ #
                # Se ci sono regole dinamiche attive su questo link
                # premium, le rimuoviamo immediatamente per garantire
                # la massima banda al traffico video
                if self.premium_links[dpid]['dynamic_active']:
                    self.logger.info(
                        "*** PREEMPTION IMMEDIATA su S%d "
                        "causata dall'arrivo del video! ***", dpid
                    )
                    self._remove_dynamic_flows(dpid)

            # ---------------------------------------------------- #
            #  Gestione anche per S1: determinare il link premium   #
            #  corretto in base alla destinazione                   #
            # ---------------------------------------------------- #
            # Se il video arriva su S1, determiniamo se va verso
            # il percorso superiore (H3 -> S2) o inferiore (H4 -> S4)
            if dpid == 1:
                if eth.dst == self.H3_MAC and 2 in self.premium_links:
                    self.premium_links[2]['video_active'] = True
                    self.logger.info("Link premium S2 marcato come VIDEO ATTIVO (da S1)")
                    if self.premium_links[2]['dynamic_active']:
                        self.logger.info("*** PREEMPTION IMMEDIATA su S2! ***")
                        self._remove_dynamic_flows(2)
                elif eth.dst == self.H4_MAC and 4 in self.premium_links:
                    self.premium_links[4]['video_active'] = True
                    self.logger.info("Link premium S4 marcato come VIDEO ATTIVO (da S1)")
                    if self.premium_links[4]['dynamic_active']:
                        self.logger.info("*** PREEMPTION IMMEDIATA su S4! ***")
                        self._remove_dynamic_flows(4)

        # ------------------------------------------------------------ #
        #  GESTIONE FINE TRAFFICO VIDEO                                #
        # ------------------------------------------------------------ #
        # Se riceviamo un pacchetto non-video su uno switch con link premium,
        # e il video era precedentemente attivo, resettiamo il flag.
        # Nota: in un sistema di produzione, useremmo un timer piu' sofisticato
        # per determinare la fine del flusso video. Qui usiamo un approccio
        # semplificato basato sull'assenza di pacchetti video nei packet-in.

        # ------------------------------------------------------------ #
        #  LOGGING DIAGNOSTICO per traffico non-video                  #
        # ------------------------------------------------------------ #
        if udp_pkt is not None and udp_pkt.dst_port != self.VIDEO_UDP_PORT:
            self.logger.debug(
                "S%d: Pacchetto UDP non-video ricevuto (porta %d -> %d) "
                "da %s verso %s",
                dpid, udp_pkt.src_port, udp_pkt.dst_port, eth.src, eth.dst
            )

        # ------------------------------------------------------------ #
        #  INOLTRO DEL PACCHETTO: Output sulla porta corretta          #
        # ------------------------------------------------------------ #
        # Per i pacchetti che arrivano al controller tramite table-miss,
        # proviamo a inoltrarli sulla porta corretta se conosciamo la
        # destinazione. Altrimenti, facciamo flooding.
        #
        # Nota: nella maggior parte dei casi, le regole installate durante
        # la configurazione gestiscono il traffico senza packet-in.
        # Questo codice gestisce i casi edge come i primi pacchetti
        # prima che le regole siano installate.

        # Determinazione della porta di uscita in base al MAC di destinazione
        dst = eth.dst
        out_port = None

        if dpid == 1:
            if dst == self.H3_MAC:
                out_port = self.PORTS[1]['to_upper']
            elif dst == self.H4_MAC:
                out_port = self.PORTS[1]['to_lower']
            elif dst == self.H1_MAC:
                out_port = self.PORTS[1]['H1']
            elif dst == self.H2_MAC:
                out_port = self.PORTS[1]['H2']
        elif dpid == 6:
            if dst == self.H3_MAC:
                out_port = self.PORTS[6]['H3']
            elif dst == self.H4_MAC:
                out_port = self.PORTS[6]['H4']

        # Se non conosciamo la porta di uscita, scartiamo il pacchetto.
        # Non usiamo OFPP_FLOOD per evitare loop causati dai premium links.
        if out_port is None:
            return

        actions = [parser.OFPActionOutput(out_port)]

        # Invio del pacchetto sullo switch con le azioni determinate
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)

    # ================================================================== #
    #  REST API: Assemblaggio JSON per la dashboard                      #
    # ================================================================== #

    def get_stats_json(self):
        """
        Assembla il dizionario JSON con tutte le statistiche per la dashboard.

        Returns:
            dict: Statistiche complete del sistema per la dashboard.
        """
        with self.stats_lock:
            port_stats_serializable = {}
            for dpid, ports in self.port_stats.items():
                port_stats_serializable[str(dpid)] = {}
                for port_no, stats in ports.items():
                    port_stats_serializable[str(dpid)][str(port_no)] = stats

            port_speed_serializable = {}
            for dpid, ports in self.port_speed.items():
                port_speed_serializable[str(dpid)] = {}
                for port_no, speed in ports.items():
                    port_speed_serializable[str(dpid)][str(port_no)] = speed

            events_copy = list(self.dashboard_events)

        dynamic_flows_count = 0
        dynamic_detail = []
        for dpid in [2, 4]:
            if dpid in self.premium_links and self.premium_links[dpid]['dynamic_active']:
                dynamic_flows_count += 1
                dynamic_detail.append('S%d' % dpid)

        video_streams = 0
        for dpid in [2, 4]:
            if dpid in self.premium_links and self.premium_links[dpid]['video_active']:
                video_streams += 1

        return {
            'timestamp': time.time(),
            'premium_links': {
                'upper': {
                    'usage_mbps': round(self.premium_links[2]['usage_mbps'], 3),
                    'capacity_mbps': self.premium_links[2]['capacity_mbps'],
                    'dynamic_active': self.premium_links[2]['dynamic_active'],
                    'video_active': self.premium_links[2]['video_active'],
                },
                'lower': {
                    'usage_mbps': round(self.premium_links[4]['usage_mbps'], 3),
                    'capacity_mbps': self.premium_links[4]['capacity_mbps'],
                    'dynamic_active': self.premium_links[4]['dynamic_active'],
                    'video_active': self.premium_links[4]['video_active'],
                }
            },
            'slice_delay': {
                'premium_one_way_ms': 13,
                'premium_hops': 3,
                'standard_one_way_ms': 110,
                'standard_hops': 4,
            },
            'video_streams': video_streams,
            'dynamic_flows_count': dynamic_flows_count,
            'dynamic_detail': dynamic_detail,
            'preemption_count': self.preemption_count,
            'events': events_copy,
            'port_stats': port_stats_serializable,
            'port_speed': port_speed_serializable,
            'switches_connected': sorted(list(self.datapaths.keys())),
        }


# ====================================================================== #
#  REST API: Controller WSGI per servire la dashboard e le statistiche   #
# ====================================================================== #

class DashboardAPI(ControllerBase):
    """
    Controller WSGI che espone le REST API per il monitoraggio.

    Endpoint:
    - GET /api/stats       -> ritorna il JSON con le statistiche in tempo reale
    """

    def __init__(self, req, link, data, **config):
        super(DashboardAPI, self).__init__(req, link, data, **config)
        self.app = data[DASHBOARD_APP_INSTANCE_NAME]

    @route('stats', '/api/stats', methods=['GET'])
    def get_stats(self, req, **kwargs):
        """Ritorna le statistiche in formato JSON."""
        stats = self.app.get_stats_json()
        body = json.dumps(stats)
        return Response(content_type='application/json', charset='utf-8', body=body)
