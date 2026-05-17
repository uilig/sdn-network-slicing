"""
Controller Ryu per la fase 2: Service Slicing con Premium Links.

Il traffico UDP 9999 (video) viene instradato sui Premium Links (S2→S6, S4→S6)
con priorità 200; il resto passa per i percorsi standard (S2→S3→S6, S4→S5→S6)
con priorità 100. Tutti gli host possono comunicare tra loro, a differenza
della fase 1. Le regole vengono installate proattivamente alla connessione
degli switch, non on-demand.
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.ofproto import ofproto_v1_3


class ServiceSlicingController(app_manager.RyuApp):
    """
    Controller SDN Ryu per Service Slicing con Premium Links.

    Questa classe implementa un controller SDN che gestisce una topologia
    a 6 switch, instradando il traffico video (UDP porta 9999) attraverso
    link premium dedicati e tutto il restante traffico attraverso percorsi
    standard con colli di bottiglia.

    Il controller installa regole proattive su ogni switch al momento della
    connessione, garantendo instradamento deterministico e a bassa latenza
    senza necessita' di consultare il controller per ogni flusso.

    Attributes:
        OFP_VERSIONS: Lista delle versioni OpenFlow supportate (solo 1.3).
    """

    # -----------------------------------------------------------------------
    # Specifica della versione OpenFlow utilizzata dal controller.
    # OpenFlow 1.3 e' richiesto per il supporto completo di match multipli,
    # gruppi, meters e tabelle multiple.
    # -----------------------------------------------------------------------
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """
        Inizializzazione del controller Service Slicing.

        Configura tutte le costanti necessarie per il funzionamento del
        controller: indirizzi MAC degli host, mappatura delle porte per
        ogni switch e livelli di priorita' delle regole OpenFlow.
        """
        super(ServiceSlicingController, self).__init__(*args, **kwargs)

        # -------------------------------------------------------------------
        # INDIRIZZI MAC DEGLI HOST
        # Definizione degli indirizzi MAC di tutti gli host nella rete.
        # Questi indirizzi vengono usati come match nelle flow entries
        # per determinare la porta di uscita corretta su ogni switch.
        # -------------------------------------------------------------------
        self.MAC_H1 = "00:00:00:00:00:01"  # MAC del server H1 (sorgente upper)
        self.MAC_H2 = "00:00:00:00:00:02"  # MAC del server H2 (sorgente lower)
        self.MAC_H3 = "00:00:00:00:00:03"    # MAC dell'host H3 (client upper)
        self.MAC_H4 = "00:00:00:00:00:04"    # MAC dell'host H4 (client lower)

        # -------------------------------------------------------------------
        # MAPPATURA DELLE PORTE PER OGNI SWITCH
        # Ogni switch ha una configurazione specifica delle porte fisiche.
        # L'uso di un dizionario con chiavi descrittive migliora la
        # leggibilita' del codice e riduce il rischio di errori.
        #
        # Le porte sono nominate in base alla loro funzione:
        # - "to_X" / "from_X": indica la direzione verso/dallo switch X
        # - "standard": indica un link standard (collo di bottiglia)
        # - "premium": indica un link premium (alta capacita')
        # - Nomi host: indica la porta collegata direttamente all'host
        # -------------------------------------------------------------------
        self.PORTS = {
            # S1 - Switch di accesso lato sorgente
            # Collega le due sorgenti (H1, H2) ai due percorsi (upper e lower)
            1: {
                "H1": 1,        # Porta collegata al server H1
                "H2": 2,        # Porta collegata al server H2
                "to_upper": 3,    # Porta verso il percorso upper (S2)
                "to_lower": 4,    # Porta verso il percorso lower (S4)
            },
            # S2 - Switch di distribuzione upper
            # Smista il traffico tra percorso standard (via S3) e premium (diretto a S6)
            2: {
                "to_s1": 1,              # Porta verso S1 (ritorno alla sorgente)
                "to_s3_standard": 2,     # Porta verso S3 (percorso standard con bottleneck)
                "to_s6_premium": 3,      # Porta verso S6 (link premium diretto)
            },
            # S3 - Switch intermedio upper (collo di bottiglia)
            # Agisce come semplice forwarding switch bidirezionale
            3: {
                "to_s2": 1,   # Porta verso S2 (direzione sorgente)
                "to_s6": 2,   # Porta verso S6 (direzione destinazione)
            },
            # S4 - Switch di distribuzione lower
            # Analogo a S2, smista traffico standard e premium per il percorso lower
            4: {
                "to_s1": 1,              # Porta verso S1 (ritorno alla sorgente)
                "to_s5_standard": 2,     # Porta verso S5 (percorso standard con bottleneck)
                "to_s6_premium": 3,      # Porta verso S6 (link premium diretto)
            },
            # S5 - Switch intermedio lower (collo di bottiglia)
            # Analogo a S3, semplice forwarding bidirezionale
            5: {
                "to_s4": 1,   # Porta verso S4 (direzione sorgente)
                "to_s6": 2,   # Porta verso S6 (direzione destinazione)
            },
            # S6 - Switch di accesso lato destinazione
            # Riceve traffico da tutti i percorsi (standard e premium) e lo
            # consegna agli host H3 e H4
            6: {
                "from_s3_standard": 1,   # Porta da S3 (arrivo standard upper)
                "from_s2_premium": 2,    # Porta da S2 (arrivo premium upper)
                "H3": 3,                 # Porta collegata all'host H3
                "from_s5_standard": 4,   # Porta da S5 (arrivo standard lower)
                "from_s4_premium": 5,    # Porta da S4 (arrivo premium lower)
                "H4": 6,                 # Porta collegata all'host H4
            },
        }

        # -------------------------------------------------------------------
        # LIVELLI DI PRIORITA' DELLE REGOLE OPENFLOW
        # Le priorita' determinano l'ordine di valutazione delle regole
        # nella flow table di ogni switch. Una priorita' piu' alta significa
        # che la regola viene valutata prima.
        #
        # Il sistema a 5 livelli garantisce che:
        # 1. Il traffico video venga sempre catturato prima del default
        # 2. Il traffico normale venga instradato correttamente
        # 3. L'ARP funzioni per la risoluzione degli indirizzi
        # 4. L'IPv6 venga scartato per evitare interferenze
        # 5. Qualsiasi pacchetto residuo venga inviato al controller
        # -------------------------------------------------------------------
        self.VIDEO_PRIORITY = 200    # Massima priorita': traffico video UDP 9999
        self.DEFAULT_PRIORITY = 100  # Priorita' media: instradamento basato su MAC
        self.ARP_PRIORITY = 50       # Priorita' ARP: flooding per address resolution
        self.IPV6_DROP_PRIORITY = 5  # Priorita' bassa: scarto pacchetti IPv6
        self.TABLE_MISS_PRIORITY = 0 # Priorita' minima: fallback al controller

        self.logger.info("==============================================")
        self.logger.info("  Service Slicing Controller - SDN Network Slicing")
        self.logger.info("==============================================")
        self.logger.info("Controller inizializzato con successo.")
        self.logger.info("Traffico VIDEO (UDP 9999) -> Link PREMIUM")
        self.logger.info("Traffico NORMALE          -> Link STANDARD")

    # =======================================================================
    # METODI DI UTILITA' PER L'INSTALLAZIONE DELLE FLOW ENTRIES
    # =======================================================================

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0,
                 hard_timeout=0):
        """
        Installa una singola flow entry nella flow table di uno switch.

        Questo metodo di utilita' semplifica l'installazione delle regole
        OpenFlow costruendo automaticamente il messaggio OFPFlowMod con
        i parametri specificati.

        Args:
            datapath: Oggetto datapath che rappresenta la connessione con lo
                      switch. Contiene il riferimento al protocollo OpenFlow
                      e al parser dei messaggi.
            priority: Livello di priorita' della regola (0-65535). Le regole
                      con priorita' piu' alta vengono valutate per prime.
            match: Oggetto OFPMatch che definisce i criteri di corrispondenza
                   per i pacchetti (es. MAC destinazione, porta UDP, ecc.).
            actions: Lista di azioni da eseguire sui pacchetti corrispondenti
                     (es. OFPActionOutput per inviare su una porta specifica).
            idle_timeout: Tempo in secondi dopo il quale la regola viene
                          rimossa se non ci sono pacchetti corrispondenti.
                          0 = nessun timeout (regola permanente).
            hard_timeout: Tempo in secondi dopo il quale la regola viene
                          rimossa incondizionatamente.
                          0 = nessun timeout (regola permanente).
        """
        # Ottenimento dei riferimenti al protocollo e al parser OpenFlow
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Costruzione dell'istruzione "apply actions" che esegue le azioni
        # specificate nell'ordine indicato
        instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]

        # Costruzione e invio del messaggio FlowMod allo switch
        # FlowMod e' il messaggio OpenFlow utilizzato per aggiungere,
        # modificare o eliminare flow entries dalla flow table
        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        datapath.send_msg(flow_mod)

    def install_table_miss(self, datapath):
        """
        Installa la regola table-miss sullo switch.

        La regola table-miss e' una flow entry con priorita' 0 e match vuoto
        (corrisponde a qualsiasi pacchetto). Quando un pacchetto non corrisponde
        a nessuna altra regola nella flow table, viene catturato da questa
        regola e inviato al controller tramite un messaggio packet-in.

        Questo garantisce che nessun pacchetto venga scartato silenziosamente
        e che il controller possa gestire eventuali casi non previsti.

        Args:
            datapath: Oggetto datapath dello switch su cui installare la regola.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Match vuoto: corrisponde a qualsiasi pacchetto
        match = parser.OFPMatch()

        # Azione: invia il pacchetto al controller con buffer completo
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(datapath, self.TABLE_MISS_PRIORITY, match, actions)
        self.logger.info("  [S%s] Regola table-miss installata (priority=%d)",
                         datapath.id, self.TABLE_MISS_PRIORITY)

    def install_arp_flood(self, datapath):
        """
        Installa regole di forwarding ARP specifiche per ogni switch.

        NOTA IMPORTANTE: Non si puo usare OFPP_FLOOD su questa topologia
        perche i Premium Links creano cicli (S2-S6 e S4-S6). Un broadcast
        ARP in flood causerebbe una tempesta di pacchetti.

        La soluzione e installare regole ARP per ogni porta di ingresso
        che inoltrano il broadcast solo sulle porte corrette, usando
        ESCLUSIVAMENTE i percorsi standard (non i premium links).

        Schema di forwarding ARP broadcast per switch:
        - S1: porta 1(H1)->3,4  porta 2(H2)->3,4  porta 3(S2)->1,2,4  porta 4(S4)->1,2,3
        - S2: porta 1(S1)->2  porta 2(S3)->1  (NO porta 3 premium)
        - S3: porta 1(S2)->2  porta 2(S6)->1
        - S4: porta 1(S1)->2  porta 2(S5)->1  (NO porta 3 premium)
        - S5: porta 1(S4)->2  porta 2(S6)->1
        - S6: porta 1(S3)->3,4,6  porta 2->nessuno(premium)  porta 3(H3)->1,4,6
              porta 4(S5)->1,3,6  porta 5->nessuno(premium)  porta 6(H4)->1,3,4

        Args:
            datapath: Oggetto datapath dello switch.
        """
        parser = datapath.ofproto_parser
        dpid = datapath.id

        # Mappa di forwarding ARP: per ogni switch e per ogni porta di ingresso,
        # definisce le porte di uscita per i broadcast ARP.
        # I premium links (S2:3, S4:3, S6:2, S6:5) sono ESCLUSI per evitare cicli.
        arp_forward = {
            1: {1: [3, 4], 2: [3, 4], 3: [1, 2, 4], 4: [1, 2, 3]},
            2: {1: [2], 2: [1]},
            3: {1: [2], 2: [1]},
            4: {1: [2], 2: [1]},
            5: {1: [2], 2: [1]},
            6: {1: [3, 4, 6], 3: [1, 4, 6], 4: [1, 3, 6], 6: [1, 3, 4]},
        }

        if dpid in arp_forward:
            for in_port, out_ports in arp_forward[dpid].items():
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_ARP,
                    in_port=in_port
                )
                actions = [parser.OFPActionOutput(p) for p in out_ports]
                self.add_flow(datapath, self.ARP_PRIORITY, match, actions)

        self.logger.info("  [S%s] Regole ARP installate (no flood, no premium)",
                         dpid)

    def install_ipv6_drop(self, datapath):
        """
        Installa la regola per scartare i pacchetti IPv6.

        I pacchetti IPv6 (EtherType 0x86DD) vengono scartati silenziosamente
        per evitare che traffico non necessario appesantisca la rete.
        In questa topologia si utilizza esclusivamente IPv4, quindi i
        pacchetti IPv6 (spesso generati automaticamente dai sistemi operativi
        per neighbor discovery, router solicitation, ecc.) sono indesiderati.

        La regola ha una lista di azioni vuota, il che significa che i
        pacchetti corrispondenti vengono semplicemente scartati (drop).

        Args:
            datapath: Oggetto datapath dello switch su cui installare la regola.
        """
        parser = datapath.ofproto_parser

        # Match: pacchetti con EtherType IPv6 (0x86DD)
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6)

        # Azioni vuote = DROP (il pacchetto viene scartato senza essere
        # inoltrato su nessuna porta ne' inviato al controller)
        actions = []

        self.add_flow(datapath, self.IPV6_DROP_PRIORITY, match, actions)
        self.logger.info("  [S%s] Regola drop IPv6 installata (priority=%d)",
                         datapath.id, self.IPV6_DROP_PRIORITY)

    # =======================================================================
    # METODI PER L'INSTALLAZIONE DELLE REGOLE SPECIFICHE PER OGNI SWITCH
    # =======================================================================

    def install_s1_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S1.

        S1 e' lo switch di accesso lato sorgente. E' connesso ai due server
        sorgente (H1 sulla porta 1, H2 sulla porta 2) e ai due percorsi
        di distribuzione (upper via S2 sulla porta 3, lower via S4 sulla porta 4).

        Regole installate:
        - Traffico verso H1 (MAC 00:00:00:00:00:01) -> Porta 1
        - Traffico verso H2 (MAC 00:00:00:00:00:02) -> Porta 2
        - Traffico verso H3  (MAC 00:00:00:00:00:03) -> Porta 3 (percorso upper)
        - Traffico verso H4  (MAC 00:00:00:00:00:04) -> Porta 4 (percorso lower)

        Su S1 non e' necessario distinguere tra traffico video e normale
        perche' la decisione sul percorso (premium vs standard) viene presa
        dagli switch di distribuzione S2 e S4.

        Args:
            datapath: Oggetto datapath dello switch S1.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[1]

        self.logger.info("  [S1] Installazione regole di instradamento...")

        # Regola: traffico destinato a H1 -> porta collegata a H1
        match_h1 = parser.OFPMatch(eth_dst=self.MAC_H1)
        actions_h1 = [parser.OFPActionOutput(ports["H1"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h1, actions_h1)
        self.logger.info("  [S1] eth_dst=%s -> porta %d (H1)",
                         self.MAC_H1, ports["H1"])

        # Regola: traffico destinato a H2 -> porta collegata a H2
        match_h2 = parser.OFPMatch(eth_dst=self.MAC_H2)
        actions_h2 = [parser.OFPActionOutput(ports["H2"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h2, actions_h2)
        self.logger.info("  [S1] eth_dst=%s -> porta %d (H2)",
                         self.MAC_H2, ports["H2"])

        # Regola: traffico destinato a H3 -> porta verso percorso upper (S2)
        # H3 si trova sul lato destinazione raggiungibile tramite il percorso
        # upper (S1 -> S2 -> S3/S6 -> S6 -> H3)
        match_h3 = parser.OFPMatch(eth_dst=self.MAC_H3)
        actions_h3 = [parser.OFPActionOutput(ports["to_upper"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h3, actions_h3)
        self.logger.info("  [S1] eth_dst=%s -> porta %d (percorso UPPER verso H3)",
                         self.MAC_H3, ports["to_upper"])

        # Regola: traffico destinato a H4 -> porta verso percorso lower (S4)
        # H4 si trova sul lato destinazione raggiungibile tramite il percorso
        # lower (S1 -> S4 -> S5/S6 -> S6 -> H4)
        match_h4 = parser.OFPMatch(eth_dst=self.MAC_H4)
        actions_h4 = [parser.OFPActionOutput(ports["to_lower"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h4, actions_h4)
        self.logger.info("  [S1] eth_dst=%s -> porta %d (percorso LOWER verso H4)",
                         self.MAC_H4, ports["to_lower"])

    def install_s2_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S2.

        S2 e' lo switch di distribuzione del percorso upper. E' il punto
        in cui avviene la decisione cruciale di service slicing: il traffico
        video (UDP porta 9999) viene diretto sul link premium verso S6,
        mentre tutto il resto viene instradato sul percorso standard via S3.

        Regole installate:
        - Traffico verso H1 (qualsiasi tipo) -> Porta 1 (ritorno a S1)
        - Traffico VIDEO verso H3 (UDP 9999) -> Porta 3 (PREMIUM, priority 200)
        - Traffico DEFAULT verso H3 -> Porta 2 (standard via S3, priority 100)

        La differenza di priorita' (200 vs 100) garantisce che il traffico
        video venga catturato dalla regola premium prima di corrispondere
        alla regola default, poiche' entrambe hanno lo stesso eth_dst ma
        la regola video ha un match piu' specifico e priorita' superiore.

        Args:
            datapath: Oggetto datapath dello switch S2.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[2]

        self.logger.info("  [S2] Installazione regole di instradamento...")

        # Regola: traffico destinato a H1 -> ritorno verso S1
        # Gestisce il traffico di ritorno (es. risposte da H3 verso H1)
        match_h1 = parser.OFPMatch(eth_dst=self.MAC_H1)
        actions_h1 = [parser.OFPActionOutput(ports["to_s1"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h1, actions_h1)
        self.logger.info("  [S2] eth_dst=%s -> porta %d (ritorno a S1)",
                         self.MAC_H1, ports["to_s1"])

        # Regola VIDEO: traffico UDP porta 9999 verso H3 -> LINK PREMIUM
        # Questa regola ha priorita' 200, superiore alla regola default.
        # Match: IPv4 + UDP + porta destinazione 9999 + MAC destinazione H3
        match_video_h3 = parser.OFPMatch(
            eth_type=0x0800,          # EtherType IPv4
            ip_proto=17,              # Protocollo UDP (numero 17)
            udp_dst=9999,             # Porta destinazione UDP 9999 (video)
            eth_dst=self.MAC_H3       # Destinazione: host H3
        )
        actions_video_h3 = [parser.OFPActionOutput(ports["to_s6_premium"])]
        self.add_flow(datapath, self.VIDEO_PRIORITY, match_video_h3,
                      actions_video_h3)
        self.logger.info("  [S2] VIDEO (UDP 9999) eth_dst=%s -> porta %d "
                         "(PREMIUM link verso S6, priority=%d)",
                         self.MAC_H3, ports["to_s6_premium"],
                         self.VIDEO_PRIORITY)

        # Regola DEFAULT: traffico generico verso H3 -> percorso standard via S3
        # Questa regola ha priorita' 100, inferiore alla regola video.
        # Cattura tutto il traffico destinato a H3 che non e' video.
        match_default_h3 = parser.OFPMatch(eth_dst=self.MAC_H3)
        actions_default_h3 = [parser.OFPActionOutput(ports["to_s3_standard"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_default_h3,
                      actions_default_h3)
        self.logger.info("  [S2] DEFAULT eth_dst=%s -> porta %d "
                         "(STANDARD via S3, priority=%d)",
                         self.MAC_H3, ports["to_s3_standard"],
                         self.DEFAULT_PRIORITY)

    def install_s3_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S3.

        S3 e' lo switch intermedio del percorso upper standard. Funziona
        come un semplice forwarding switch bidirezionale, inoltrando il
        traffico tra S2 (porta 1) e S6 (porta 2) in entrambe le direzioni.

        Questo switch rappresenta il "collo di bottiglia" del percorso
        upper standard. Il link che lo attraversa ha banda limitata,
        motivo per cui il traffico video viene dirottato sul link premium.

        Regole installate:
        - Pacchetti in ingresso dalla porta 1 (da S2) -> porta 2 (verso S6)
        - Pacchetti in ingresso dalla porta 2 (da S6) -> porta 1 (verso S2)

        Le regole sono basate sulla porta di ingresso (in_port) anziche'
        sul MAC destinazione, rendendo S3 un forwarding trasparente.

        Args:
            datapath: Oggetto datapath dello switch S3.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[3]

        self.logger.info("  [S3] Installazione regole di instradamento...")

        # Regola: traffico da S2 (porta 1) -> verso S6 (porta 2)
        # Direzione: sorgente -> destinazione (forward path)
        match_from_s2 = parser.OFPMatch(in_port=ports["to_s2"])
        actions_to_s6 = [parser.OFPActionOutput(ports["to_s6"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_from_s2,
                      actions_to_s6)
        self.logger.info("  [S3] in_port=%d (da S2) -> porta %d (verso S6)",
                         ports["to_s2"], ports["to_s6"])

        # Regola: traffico da S6 (porta 2) -> verso S2 (porta 1)
        # Direzione: destinazione -> sorgente (return path)
        match_from_s6 = parser.OFPMatch(in_port=ports["to_s6"])
        actions_to_s2 = [parser.OFPActionOutput(ports["to_s2"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_from_s6,
                      actions_to_s2)
        self.logger.info("  [S3] in_port=%d (da S6) -> porta %d (verso S2)",
                         ports["to_s6"], ports["to_s2"])

    def install_s4_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S4.

        S4 e' lo switch di distribuzione del percorso lower, analogo a S2
        per il percorso upper. Gestisce il service slicing per il traffico
        diretto a H2: il traffico video (UDP porta 9999) viene diretto
        sul link premium verso S6, mentre il resto va su S5 (standard).

        Regole installate:
        - Traffico verso H2 (qualsiasi tipo) -> Porta 1 (ritorno a S1)
        - Traffico VIDEO verso H4 (UDP 9999) -> Porta 3 (PREMIUM, priority 200)
        - Traffico DEFAULT verso H4 -> Porta 2 (standard via S5, priority 100)

        Args:
            datapath: Oggetto datapath dello switch S4.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[4]

        self.logger.info("  [S4] Installazione regole di instradamento...")

        # Regola: traffico destinato a H2 -> ritorno verso S1
        # Gestisce il traffico di ritorno (es. risposte da H4 verso H2)
        match_h2 = parser.OFPMatch(eth_dst=self.MAC_H2)
        actions_h2 = [parser.OFPActionOutput(ports["to_s1"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h2, actions_h2)
        self.logger.info("  [S4] eth_dst=%s -> porta %d (ritorno a S1)",
                         self.MAC_H2, ports["to_s1"])

        # Regola VIDEO: traffico UDP porta 9999 verso H4 -> LINK PREMIUM
        # Analogo alla regola video di S2, ma per il percorso lower
        match_video_h4 = parser.OFPMatch(
            eth_type=0x0800,          # EtherType IPv4
            ip_proto=17,              # Protocollo UDP
            udp_dst=9999,             # Porta destinazione UDP 9999 (video)
            eth_dst=self.MAC_H4       # Destinazione: host H4
        )
        actions_video_h4 = [parser.OFPActionOutput(ports["to_s6_premium"])]
        self.add_flow(datapath, self.VIDEO_PRIORITY, match_video_h4,
                      actions_video_h4)
        self.logger.info("  [S4] VIDEO (UDP 9999) eth_dst=%s -> porta %d "
                         "(PREMIUM link verso S6, priority=%d)",
                         self.MAC_H4, ports["to_s6_premium"],
                         self.VIDEO_PRIORITY)

        # Regola DEFAULT: traffico generico verso H4 -> percorso standard via S5
        match_default_h4 = parser.OFPMatch(eth_dst=self.MAC_H4)
        actions_default_h4 = [parser.OFPActionOutput(ports["to_s5_standard"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_default_h4,
                      actions_default_h4)
        self.logger.info("  [S4] DEFAULT eth_dst=%s -> porta %d "
                         "(STANDARD via S5, priority=%d)",
                         self.MAC_H4, ports["to_s5_standard"],
                         self.DEFAULT_PRIORITY)

    def install_s5_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S5.

        S5 e' lo switch intermedio del percorso lower standard, analogo a S3
        per il percorso upper. Funziona come forwarding bidirezionale tra
        S4 (porta 1) e S6 (porta 2).

        Rappresenta il collo di bottiglia del percorso lower standard.

        Regole installate:
        - Pacchetti in ingresso dalla porta 1 (da S4) -> porta 2 (verso S6)
        - Pacchetti in ingresso dalla porta 2 (da S6) -> porta 1 (verso S4)

        Args:
            datapath: Oggetto datapath dello switch S5.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[5]

        self.logger.info("  [S5] Installazione regole di instradamento...")

        # Regola: traffico da S4 (porta 1) -> verso S6 (porta 2)
        match_from_s4 = parser.OFPMatch(in_port=ports["to_s4"])
        actions_to_s6 = [parser.OFPActionOutput(ports["to_s6"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_from_s4,
                      actions_to_s6)
        self.logger.info("  [S5] in_port=%d (da S4) -> porta %d (verso S6)",
                         ports["to_s4"], ports["to_s6"])

        # Regola: traffico da S6 (porta 2) -> verso S4 (porta 1)
        match_from_s6 = parser.OFPMatch(in_port=ports["to_s6"])
        actions_to_s4 = [parser.OFPActionOutput(ports["to_s4"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_from_s6,
                      actions_to_s4)
        self.logger.info("  [S5] in_port=%d (da S6) -> porta %d (verso S4)",
                         ports["to_s6"], ports["to_s4"])

    def install_s6_rules(self, datapath):
        """
        Installa le regole di instradamento per lo switch S6.

        S6 e' lo switch di accesso lato destinazione, il piu' complesso della
        topologia. Riceve traffico da quattro direzioni diverse (standard upper,
        premium upper, standard lower, premium lower) e lo consegna agli host
        H3 e H4. Gestisce anche il traffico di ritorno verso le sorgenti
        H1 e H2, applicando il service slicing anche in direzione inversa.

        Regole installate:
        - Traffico verso H3 -> Porta 3 (consegna diretta a H3)
        - Traffico verso H4 -> Porta 6 (consegna diretta a H4)
        - Traffico VIDEO verso H1 (UDP 9999) -> Porta 2 (premium upper, priority 200)
        - Traffico DEFAULT verso H1 -> Porta 1 (standard upper via S3, priority 100)
        - Traffico VIDEO verso H2 (UDP 9999) -> Porta 5 (premium lower, priority 200)
        - Traffico DEFAULT verso H2 -> Porta 4 (standard lower via S5, priority 100)

        Il service slicing viene applicato anche nel percorso di ritorno
        (da H3/H4 verso H1/H2) per garantire simmetria nei flussi
        e mantenere la separazione del traffico video in entrambe le direzioni.

        Args:
            datapath: Oggetto datapath dello switch S6.
        """
        parser = datapath.ofproto_parser
        ports = self.PORTS[6]

        self.logger.info("  [S6] Installazione regole di instradamento...")

        # -------------------------------------------------------------------
        # REGOLE DI CONSEGNA LOCALE (verso gli host direttamente connessi)
        # -------------------------------------------------------------------

        # Regola: traffico destinato a H3 -> porta collegata a H3
        match_h3 = parser.OFPMatch(eth_dst=self.MAC_H3)
        actions_h3 = [parser.OFPActionOutput(ports["H3"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h3, actions_h3)
        self.logger.info("  [S6] eth_dst=%s -> porta %d (consegna a H3)",
                         self.MAC_H3, ports["H3"])

        # Regola: traffico destinato a H4 -> porta collegata a H4
        match_h4 = parser.OFPMatch(eth_dst=self.MAC_H4)
        actions_h4 = [parser.OFPActionOutput(ports["H4"])]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_h4, actions_h4)
        self.logger.info("  [S6] eth_dst=%s -> porta %d (consegna a H4)",
                         self.MAC_H4, ports["H4"])

        # -------------------------------------------------------------------
        # REGOLE DI RITORNO VERSO H1 (percorso upper)
        # Il traffico di ritorno verso H1 segue il percorso upper.
        # Video -> link premium (porta 2), Normale -> standard via S3 (porta 1)
        # -------------------------------------------------------------------

        # Regola VIDEO: traffico UDP 9999 verso H1 -> link premium upper
        match_video_h1 = parser.OFPMatch(
            eth_type=0x0800,          # EtherType IPv4
            ip_proto=17,              # Protocollo UDP
            udp_dst=9999,             # Porta destinazione UDP 9999 (video)
            eth_dst=self.MAC_H1     # Destinazione: server H1
        )
        actions_video_h1 = [parser.OFPActionOutput(ports["from_s2_premium"])]
        self.add_flow(datapath, self.VIDEO_PRIORITY, match_video_h1,
                      actions_video_h1)
        self.logger.info("  [S6] VIDEO (UDP 9999) eth_dst=%s -> porta %d "
                         "(PREMIUM upper verso S2, priority=%d)",
                         self.MAC_H1, ports["from_s2_premium"],
                         self.VIDEO_PRIORITY)

        # Regola DEFAULT: traffico generico verso H1 -> standard upper via S3
        match_default_h1 = parser.OFPMatch(eth_dst=self.MAC_H1)
        actions_default_h1 = [
            parser.OFPActionOutput(ports["from_s3_standard"])
        ]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_default_h1,
                      actions_default_h1)
        self.logger.info("  [S6] DEFAULT eth_dst=%s -> porta %d "
                         "(STANDARD upper via S3, priority=%d)",
                         self.MAC_H1, ports["from_s3_standard"],
                         self.DEFAULT_PRIORITY)

        # -------------------------------------------------------------------
        # REGOLE DI RITORNO VERSO H2 (percorso lower)
        # Il traffico di ritorno verso H2 segue il percorso lower.
        # Video -> link premium (porta 5), Normale -> standard via S5 (porta 4)
        # -------------------------------------------------------------------

        # Regola VIDEO: traffico UDP 9999 verso H2 -> link premium lower
        match_video_h2 = parser.OFPMatch(
            eth_type=0x0800,          # EtherType IPv4
            ip_proto=17,              # Protocollo UDP
            udp_dst=9999,             # Porta destinazione UDP 9999 (video)
            eth_dst=self.MAC_H2     # Destinazione: server H2
        )
        actions_video_h2 = [parser.OFPActionOutput(ports["from_s4_premium"])]
        self.add_flow(datapath, self.VIDEO_PRIORITY, match_video_h2,
                      actions_video_h2)
        self.logger.info("  [S6] VIDEO (UDP 9999) eth_dst=%s -> porta %d "
                         "(PREMIUM lower verso S4, priority=%d)",
                         self.MAC_H2, ports["from_s4_premium"],
                         self.VIDEO_PRIORITY)

        # Regola DEFAULT: traffico generico verso H2 -> standard lower via S5
        match_default_h2 = parser.OFPMatch(eth_dst=self.MAC_H2)
        actions_default_h2 = [
            parser.OFPActionOutput(ports["from_s5_standard"])
        ]
        self.add_flow(datapath, self.DEFAULT_PRIORITY, match_default_h2,
                      actions_default_h2)
        self.logger.info("  [S6] DEFAULT eth_dst=%s -> porta %d "
                         "(STANDARD lower via S5, priority=%d)",
                         self.MAC_H2, ports["from_s5_standard"],
                         self.DEFAULT_PRIORITY)

    # =======================================================================
    # GESTORI DEGLI EVENTI OPENFLOW
    # =======================================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Gestore dell'evento di connessione di uno switch al controller.

        Questo metodo viene invocato automaticamente da Ryu ogni volta che
        uno switch si connette al controller e completa lo scambio di
        features (handshake OpenFlow). E' il momento ideale per installare
        le regole proattive nella flow table dello switch.

        Il decoratore @set_ev_cls specifica:
        - ofp_event.EventOFPSwitchFeatures: tipo di evento da gestire
        - CONFIG_DISPATCHER: fase della connessione (configurazione iniziale)

        Per ogni switch connesso, il metodo:
        1. Installa la regola table-miss (priority 0)
        2. Installa la regola ARP flood (priority 50)
        3. Installa la regola drop IPv6 (priority 5)
        4. Installa le regole specifiche dello switch (priority 100/200)

        Args:
            ev: Evento contenente il messaggio SwitchFeatures ricevuto
                dallo switch. ev.msg.datapath contiene il riferimento
                all'oggetto datapath per comunicare con lo switch.
        """
        # Estrazione del datapath (connessione allo switch) dall'evento
        datapath = ev.msg.datapath
        dpid = datapath.id  # Datapath ID (identificativo unico dello switch)

        self.logger.info("----------------------------------------------")
        self.logger.info("Switch S%s connesso al controller", dpid)
        self.logger.info("----------------------------------------------")

        # -------------------------------------------------------------------
        # INSTALLAZIONE DELLE REGOLE COMUNI A TUTTI GLI SWITCH
        # Queste regole vengono installate su ogni switch indipendentemente
        # dal suo ruolo nella topologia.
        # -------------------------------------------------------------------
        self.install_table_miss(datapath)
        self.install_arp_flood(datapath)
        self.install_ipv6_drop(datapath)

        # -------------------------------------------------------------------
        # INSTALLAZIONE DELLE REGOLE SPECIFICHE PER OGNI SWITCH
        # In base al Datapath ID (che corrisponde al numero dello switch
        # nella topologia Mininet), vengono installate le regole appropriate.
        # -------------------------------------------------------------------
        if dpid == 1:
            self.install_s1_rules(datapath)
        elif dpid == 2:
            self.install_s2_rules(datapath)
        elif dpid == 3:
            self.install_s3_rules(datapath)
        elif dpid == 4:
            self.install_s4_rules(datapath)
        elif dpid == 5:
            self.install_s5_rules(datapath)
        elif dpid == 6:
            self.install_s6_rules(datapath)
        else:
            # Switch non riconosciuto: viene registrato un avviso nel log
            self.logger.warning("  Switch S%s non riconosciuto nella topologia! "
                                "Nessuna regola specifica installata.", dpid)

        self.logger.info("  [S%s] Configurazione completata con successo.", dpid)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Gestore dell'evento PacketIn (pacchetto inviato al controller).

        Questo metodo viene invocato quando uno switch riceve un pacchetto
        che corrisponde alla regola table-miss (priority 0) e lo inoltra
        al controller. In un controller con regole completamente proattive
        come questo, i PacketIn dovrebbero essere rari e indicano
        generalmente traffico non previsto.

        Il metodo registra un messaggio di debug con le informazioni
        sul pacchetto ricevuto per facilitare il troubleshooting.

        Args:
            ev: Evento contenente il messaggio PacketIn con il pacchetto
                ricevuto dallo switch.
        """
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']

        # Decodifica del pacchetto per estrarre informazioni utili
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # Ignora i pacchetti LLDP (Link Layer Discovery Protocol) usati
        # internamente da Ryu per la discovery della topologia
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        self.logger.debug("PacketIn su S%s: in_port=%s, src=%s, dst=%s, "
                          "ethertype=0x%04x",
                          dpid, in_port, eth.src, eth.dst, eth.ethertype)
