#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Controller Ryu per la fase 1: Topology Slicing.

Due slice completamente isolate: upper (H1<->H3) e lower (H2<->H4).
I Premium Links non vengono usati in questa fase, il traffico passa
solo per i percorsi standard (S2-S3-S6 e S4-S5-S6).

Il traffico cross-slice viene droppato silenziosamente; l'ARP broadcast
è confinato all'interno di ogni slice.
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import arp
from ryu.lib.packet import ipv4

class TopologySlicingController(app_manager.RyuApp):

    # ==========================================================================
    # VERSIONE OPENFLOW
    # ==========================================================================
    # Utilizziamo esclusivamente OpenFlow 1.3 per compatibilita con le
    # funzionalita avanzate di matching (match flessibili su qualsiasi campo),
    # multiple table, metering e group table. Questa e la versione minima
    # richiesta per un controller SDN moderno.
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ==========================================================================
    # COSTANTI DI CONFIGURAZIONE
    # ==========================================================================

    # --------------------------------------------------------------------------
    # MAC ADDRESS DEGLI HOST
    # --------------------------------------------------------------------------
    # Questi MAC address sono statici e devono corrispondere ESATTAMENTE
    # alla configurazione degli host in topology.py. Qualsiasi discrepanza
    # causerebbe il fallimento del routing e dell'isolamento tra slice.
    # In topology.py gli host sono creati con autoSetMacs=False e MAC espliciti.

    H1_MAC = '00:00:00:00:00:01'  # H1: Content Delivery Network 1 (Upper Slice)
    H2_MAC = '00:00:00:00:00:02'  # H2: Content Delivery Network 2 (Lower Slice)
    H3_MAC   = '00:00:00:00:00:03'  # H3: Client dell'Upper Slice
    H4_MAC   = '00:00:00:00:00:04'  # H4: Client del Lower Slice

    # --------------------------------------------------------------------------
    # DEFINIZIONE DEGLI SLICE
    # --------------------------------------------------------------------------
    # Ciascuno slice e definito come un insieme (set) di MAC address.
    # Gli host che condividono lo stesso insieme possono comunicare tra loro;
    # la comunicazione tra host appartenenti a insiemi diversi e BLOCCATA.
    #
    # La scelta di usare set piuttosto che liste e motivata dalla complessita
    # computazionale: il test di appartenenza (operatore 'in') su un set ha
    # complessita O(1) in media, rispetto a O(n) per una lista. Sebbene con
    # soli 2 elementi la differenza sia trascurabile, e una buona pratica
    # di programmazione.

    # Upper Slice: percorso H1 -> S1 -> S2 -> S3 -> S6 -> H3
    # Questo slice trasporta il traffico dal server H1 al client H3
    # attraverso il percorso standard superiore della topologia.
    UPPER_SLICE = {
        '00:00:00:00:00:01',  # H1 - Server sorgente
        '00:00:00:00:00:03'   # H3   - Client destinatario
    }

    # Lower Slice: percorso H2 -> S1 -> S4 -> S5 -> S6 -> H4
    # Questo slice trasporta il traffico dal server H2 al client H4
    # attraverso il percorso standard inferiore della topologia.
    LOWER_SLICE = {
        '00:00:00:00:00:02',  # H2 - Server sorgente
        '00:00:00:00:00:04'   # H4   - Client destinatario
    }

    # --------------------------------------------------------------------------
    # ROUTING STATICO (MAC -> PORTA) - SOLO PERCORSI STANDARD
    # --------------------------------------------------------------------------
    # Questa struttura dati e il cuore del forwarding: per ogni switch
    # (identificato dal suo DPID), definisce su quale porta fisica inoltrare
    # un pacchetto in base al MAC address di destinazione.
    #
    # IMPORTANTE: Le porte dei Premium Links NON compaiono in questa tabella.
    # In particolare:
    #   - S2 porta 3 (verso S6 premium) -> NON usata
    #   - S4 porta 3 (verso S6 premium) -> NON usata
    #   - S6 porta 2 (da S2 premium)    -> NON usata
    #   - S6 porta 5 (da S4 premium)    -> NON usata
    #
    # Questo garantisce che tutto il traffico attraversi i colli di bottiglia
    # (S3 per upper, S5 per lower), come previsto dal Topology Slicing.
    #
    # Formato: {dpid: {mac_destinazione: porta_uscita}}
    #
    # Percorso Upper Slice (standard):
    #   H1 -> S1(p3) -> S2(p2) -> S3(p2) -> S6(p3) -> H3
    #   H3   -> S6(p1) -> S3(p1) -> S2(p1) -> S1(p1) -> H1
    #
    # Percorso Lower Slice (standard):
    #   H2 -> S1(p4) -> S4(p2) -> S5(p2) -> S6(p6) -> H4
    #   H4   -> S6(p4) -> S5(p1) -> S4(p1) -> S1(p2) -> H2

    MAC_TO_PORT = {

        # S1 - Hub di Ingresso (DPID = 1)
        # --------------------------------
        # Switch centrale che raccoglie il traffico da entrambe le sorgenti (H1, H2)
        # e lo smista verso lo slice appropriato.
        # Connesso a: H1 (porta 1), H2 (porta 2), S2 (porta 3), S4 (porta 4)
        #
        # Per i pacchetti destinati a H1 o H2, li consegna direttamente
        # sulle porte locali (1 e 2). Per H3 li invia verso l'upper slice
        # (porta 3, via S2). Per H4 li invia verso il lower slice (porta 4, via S4).
        1: {
            '00:00:00:00:00:01': 1,  # H1 -> porta 1 (connesso localmente)
            '00:00:00:00:00:02': 2,  # H2 -> porta 2 (connesso localmente)
            '00:00:00:00:00:03': 3,  # H3   -> porta 3 (via S2, percorso upper standard)
            '00:00:00:00:00:04': 4,  # H4   -> porta 4 (via S4, percorso lower standard)
        },

        # S2 - Nodo Upper Slice (DPID = 2)
        # ---------------------------------
        # Primo switch del percorso upper dopo S1. Da qui il traffico prosegue
        # verso S3 (porta 2, percorso standard) oppure potrebbe andare
        # direttamente a S6 (porta 3, Premium Link) - ma in questa fase
        # il Premium Link NON viene utilizzato.
        # Connesso a: S1 (porta 1), S3 (porta 2), S6 (porta 3 - PREMIUM, non usata)
        #
        # Questo switch gestisce SOLO traffico dell'upper slice.
        # Le uniche destinazioni valide sono H1 (indietro verso S1) e H3
        # (avanti verso S3 via percorso standard).
        2: {
            '00:00:00:00:00:01': 1,  # H1 -> porta 1 (indietro via S1)
            '00:00:00:00:00:03': 2,  # H3   -> porta 2 (avanti via S3, standard)
            # NOTA: porta 3 (Premium Link verso S6) NON utilizzata
        },

        # S3 - Collo di Bottiglia Upper (DPID = 3)
        # -----------------------------------------
        # Switch di transito sul percorso standard dell'upper slice.
        # I link che lo attraversano sono limitati a 2 Mbps con 50ms di ritardo
        # per hop, creando il collo di bottiglia che nelle fasi successive
        # verra bypassato dal Premium Link.
        # Connesso a: S2 (porta 1), S6 (porta 2)
        #
        # Ha solo 2 porte e gestisce esclusivamente traffico upper slice.
        3: {
            '00:00:00:00:00:01': 1,  # H1 -> porta 1 (indietro via S2)
            '00:00:00:00:00:03': 2,  # H3   -> porta 2 (avanti via S6)
        },

        # S4 - Nodo Lower Slice (DPID = 4)
        # ---------------------------------
        # Equivalente simmetrico di S2 per il lower slice. Da qui il traffico
        # prosegue verso S5 (porta 2, percorso standard). La porta 3 (Premium
        # Link verso S6) NON viene utilizzata in questa fase.
        # Connesso a: S1 (porta 1), S5 (porta 2), S6 (porta 3 - PREMIUM, non usata)
        #
        # Gestisce SOLO traffico del lower slice (H2 <-> H4).
        4: {
            '00:00:00:00:00:02': 1,  # H2 -> porta 1 (indietro via S1)
            '00:00:00:00:00:04': 2,  # H4   -> porta 2 (avanti via S5, standard)
            # NOTA: porta 3 (Premium Link verso S6) NON utilizzata
        },

        # S5 - Collo di Bottiglia Lower (DPID = 5)
        # -----------------------------------------
        # Equivalente simmetrico di S3 per il lower slice. Anche qui i link
        # sono limitati a 2 Mbps con 50ms di ritardo, creando il collo di
        # bottiglia del percorso lower.
        # Connesso a: S4 (porta 1), S6 (porta 2)
        #
        # Ha solo 2 porte e gestisce esclusivamente traffico lower slice.
        5: {
            '00:00:00:00:00:02': 1,  # H2 -> porta 1 (indietro via S4)
            '00:00:00:00:00:04': 2,  # H4   -> porta 2 (avanti via S6)
        },

        # S6 - Hub di Distribuzione (DPID = 6)
        # -------------------------------------
        # Punto di convergenza di tutti i percorsi. Riceve traffico dai
        # percorsi standard (porte 1 e 4) e dai Premium Links (porte 2 e 5),
        # ma in questa fase SOLO le porte standard sono utilizzate.
        # Consegna il traffico ai client finali H3 (porta 3) e H4 (porta 6).
        #
        # Connesso a:
        #   Porta 1 -> S3 (upper standard)     - USATA
        #   Porta 2 -> S2 (PREMIUM upper)      - NON usata in topology slicing
        #   Porta 3 -> H3 (client upper)        - USATA
        #   Porta 4 -> S5 (lower standard)     - USATA
        #   Porta 5 -> S4 (PREMIUM lower)      - NON usata in topology slicing
        #   Porta 6 -> H4 (client lower)        - USATA
        #
        # ATTENZIONE: Le porte per H1 e H2 puntano verso i percorsi
        # standard (porta 1 via S3 per H1, porta 4 via S5 per H2),
        # NON verso i Premium Links.
        6: {
            '00:00:00:00:00:01': 1,  # H1 -> porta 1 (indietro via S3, upper standard)
            '00:00:00:00:00:02': 4,  # H2 -> porta 4 (indietro via S5, lower standard)
            '00:00:00:00:00:03': 3,  # H3   -> porta 3 (consegna locale al client)
            '00:00:00:00:00:04': 6,  # H4   -> porta 6 (consegna locale al client)
        },
    }

    # --------------------------------------------------------------------------
    # PORTE PER BROADCAST (ARP CONFINEMENT)
    # --------------------------------------------------------------------------
    # Questa struttura definisce, per ogni switch, quali porte fisiche
    # appartengono a ciascuno slice. Viene utilizzata esclusivamente per
    # limitare la propagazione dei pacchetti broadcast (ARP Request)
    # all'interno dello slice di appartenenza della sorgente.
    #
    # Il broadcast confinato funziona cosi:
    #   1. Un host invia un ARP Request (broadcast ff:ff:ff:ff:ff:ff)
    #   2. Lo switch riceve il pacchetto e lo invia al controller (table-miss)
    #   3. Il controller identifica lo slice della sorgente
    #   4. Il controller inoltra il broadcast SOLO sulle porte di quello slice
    #   5. La porta di ingresso viene esclusa per evitare loop
    #
    # Esempio: Se H1 (upper slice) invia un ARP su S1 porta 1, il broadcast
    # viene inoltrato solo sulla porta 3 (verso S2, upper slice), e NON sulla
    # porta 2 (H2, lower slice) ne sulla porta 4 (verso S4, lower slice).
    #
    # IMPORTANTE: Le porte dei Premium Links NON sono incluse qui.
    # Questo e coerente con la scelta di non usare i Premium Links.
    #
    # Formato: {dpid: {'upper': [porte_upper], 'lower': [porte_lower]}}

    SLICE_PORTS = {

        # S1 - Hub di Ingresso (DPID = 1)
        # H1 (porta 1) e il link verso S2 (porta 3) sono dell'upper slice.
        # H2 (porta 2) e il link verso S4 (porta 4) sono del lower slice.
        1: {
            'upper': [1, 3],  # H1 (p1) + link verso S2 (p3)
            'lower': [2, 4]   # H2 (p2) + link verso S4 (p4)
        },

        # S2 - Nodo Upper Slice (DPID = 2)
        # Tutte le porte STANDARD appartengono all'upper slice.
        # La porta 3 (Premium Link) e esclusa perche non usata.
        2: {
            'upper': [1, 2],  # S1 (p1) + S3 (p2) - solo porte standard
            'lower': []       # Nessuna porta lower su questo switch
        },

        # S3 - Collo di Bottiglia Upper (DPID = 3)
        # Tutte le porte appartengono all'upper slice.
        # Questo switch e dedicato esclusivamente al percorso upper standard.
        3: {
            'upper': [1, 2],  # S2 (p1) + S6 (p2)
            'lower': []       # Nessuna porta lower su questo switch
        },

        # S4 - Nodo Lower Slice (DPID = 4)
        # Tutte le porte STANDARD appartengono al lower slice.
        # La porta 3 (Premium Link) e esclusa perche non usata.
        4: {
            'upper': [],      # Nessuna porta upper su questo switch
            'lower': [1, 2]   # S1 (p1) + S5 (p2) - solo porte standard
        },

        # S5 - Collo di Bottiglia Lower (DPID = 5)
        # Tutte le porte appartengono al lower slice.
        # Questo switch e dedicato esclusivamente al percorso lower standard.
        5: {
            'upper': [],      # Nessuna porta upper su questo switch
            'lower': [1, 2]   # S4 (p1) + S6 (p2)
        },

        # S6 - Hub di Distribuzione (DPID = 6)
        # Porta 1 (da S3 standard) e porta 3 (H3) sono dell'upper slice.
        # Porta 4 (da S5 standard) e porta 6 (H4) sono del lower slice.
        # Le porte 2 (Premium da S2) e 5 (Premium da S4) sono ESCLUSE.
        6: {
            'upper': [1, 3],  # S3 standard (p1) + H3 (p3)
            'lower': [4, 6]   # S5 standard (p4) + H4 (p6)
        },
    }

    # --------------------------------------------------------------------------
    # PRIORITA DELLE REGOLE OPENFLOW
    # --------------------------------------------------------------------------
    # Le priorita determinano l'ordine di matching delle regole nella flow table
    # dello switch. Una priorita numericamente piu alta ha la precedenza.
    # Se due regole hanno la stessa priorita, il comportamento e indefinito
    # secondo la specifica OpenFlow 1.3.
    #
    # Schema delle priorita in questo controller:
    #   0  - Table-miss: cattura tutti i pacchetti non matchati (default)
    #   5  - Drop IPv6: scarta il traffico IPv6 prima che arrivi al controller
    #  10  - Forwarding: regole di inoltro installate dinamicamente
    # 100  - (Riservata per eventuali regole fisse in fasi future)

    PRIORITY_TABLE_MISS = 0     # Regola di default: invia al controller
    PRIORITY_DROP_IPV6 = 5      # Drop del traffico IPv6 (non gestito)
    PRIORITY_FORWARDING = 10    # Regole di forwarding installate dinamicamente
    PRIORITY_FIXED = 100        # Regole fisse (riservata per fasi future)

    # --------------------------------------------------------------------------
    # TIMEOUT DELLE FLOW RULE
    # --------------------------------------------------------------------------
    # Le regole di forwarding installate dinamicamente hanno un idle_timeout
    # di 300 secondi (5 minuti). Questo significa che se non transita traffico
    # corrispondente alla regola per 5 minuti, la regola viene rimossa
    # automaticamente dallo switch.
    #
    # L'idle_timeout e un compromesso tra:
    # - Valori bassi: piu messaggi PacketIn (piu carico sul controller)
    # - Valori alti: regole stantie che occupano memoria nella flow table
    #
    # 300 secondi e un buon valore per traffico intermittente come
    # lo streaming video, dove le pause tra i segmenti possono durare
    # diversi secondi ma raramente superano i 5 minuti.

    FLOW_IDLE_TIMEOUT = 300     # 5 minuti di idle timeout

    # ==========================================================================
    # INIZIALIZZAZIONE
    # ==========================================================================

    def __init__(self, *args, **kwargs):
        """
        Inizializza il controller Topology Slicing.

        Chiama il costruttore della classe padre (RyuApp) per inizializzare
        il framework Ryu, quindi stampa un riepilogo della configurazione
        delle slice nel log del controller.

        Il metodo __init__ viene invocato una sola volta all'avvio del
        controller (quando si esegue 'ryu-manager topology_slicing_controller.py').
        A questo punto, nessuno switch e ancora connesso.

        Args:
            *args: Argomenti posizionali passati alla classe padre RyuApp
            **kwargs: Argomenti keyword passati alla classe padre RyuApp
        """
        super(TopologySlicingController, self).__init__(*args, **kwargs)

        # Log di avvio con riepilogo completo della configurazione.
        # Questo output appare nel terminale del controller e permette
        # di verificare immediatamente che la configurazione sia corretta.
        self.logger.info("=" * 65)
        self.logger.info("TOPOLOGY SLICING CONTROLLER - SDN Network Slicing")
        self.logger.info("=" * 65)
        self.logger.info("Topologia: 6 switch con Premium Links")
        self.logger.info("Modalita: Topology Slicing (solo percorsi standard)")
        self.logger.info("-" * 65)
        self.logger.info("Configurazione Slice:")
        self.logger.info("  Upper Slice: H1 (%s) <-> H3 (%s)",
                         self.H1_MAC, self.H3_MAC)
        self.logger.info("    Percorso: H1 -> S1 -> S2 -> S3 -> S6 -> H3")
        self.logger.info("  Lower Slice: H2 (%s) <-> H4 (%s)",
                         self.H2_MAC, self.H4_MAC)
        self.logger.info("    Percorso: H2 -> S1 -> S4 -> S5 -> S6 -> H4")
        self.logger.info("-" * 65)
        self.logger.info("Premium Links: NON utilizzati (riservati per fasi 2 e 3)")
        self.logger.info("Comunicazioni Cross-Slice: BLOCCATE")
        self.logger.info("IPv6: DROP su tutti gli switch")
        self.logger.info("Flow idle_timeout: %d secondi", self.FLOW_IDLE_TIMEOUT)
        self.logger.info("=" * 65)

    # ==========================================================================
    # METODI HELPER
    # ==========================================================================

    def _get_slice(self, mac):
        """
        Determina lo slice di appartenenza di un MAC address.

        Questo metodo e il fondamento della logica di isolamento tra slice.
        Ogni decisione di forwarding, blocco o propagazione broadcast dipende
        dallo slice di appartenenza della sorgente e/o della destinazione.

        Il metodo effettua un semplice test di appartenenza (operatore 'in')
        sugli insiemi UPPER_SLICE e LOWER_SLICE. Poiche sono implementati
        come set Python, il test ha complessita O(1).

        Args:
            mac (str): Indirizzo MAC in formato stringa standard IEEE 802
                       (es. '00:00:00:00:00:01', con separatore ':')

        Returns:
            str or None: 'upper' se il MAC appartiene all'Upper Slice,
                        'lower' se appartiene al Lower Slice,
                        None se il MAC non e riconosciuto (host sconosciuto)

        Examples:
            >>> self._get_slice('00:00:00:00:00:01')  # H1
            'upper'
            >>> self._get_slice('00:00:00:00:00:04')  # H4
            'lower'
            >>> self._get_slice('aa:bb:cc:dd:ee:ff')  # MAC sconosciuto
            None
        """
        if mac in self.UPPER_SLICE:
            return 'upper'
        elif mac in self.LOWER_SLICE:
            return 'lower'
        return None

    def _is_same_slice(self, mac1, mac2):
        """
        Verifica se due MAC address appartengono allo stesso slice.

        Questa e la funzione chiave per l'isolamento tra slice. Viene chiamata
        per ogni pacchetto unicast prima di decidere se consentire o bloccare
        la comunicazione. Se i due MAC non sono nello stesso slice, il pacchetto
        viene scartato silenziosamente (drop).

        Il metodo gestisce anche il caso in cui uno o entrambi i MAC non siano
        riconosciuti (non appartengono a nessuno slice): in tal caso, la
        comunicazione viene bloccata per sicurezza (fail-closed).

        Args:
            mac1 (str): Primo MAC address (tipicamente la sorgente)
            mac2 (str): Secondo MAC address (tipicamente la destinazione)

        Returns:
            bool: True se entrambi i MAC appartengono allo stesso slice,
                  False se appartengono a slice diverse o se uno/entrambi
                  non sono riconosciuti

        Examples:
            >>> self._is_same_slice('00:00:00:00:00:01', '00:00:00:00:00:03')
            True   # H1 e H3, entrambi upper slice
            >>> self._is_same_slice('00:00:00:00:00:02', '00:00:00:00:00:04')
            True   # H2 e H4, entrambi lower slice
            >>> self._is_same_slice('00:00:00:00:00:01', '00:00:00:00:00:02')
            False  # H1 (upper) e H2 (lower), slice diverse
            >>> self._is_same_slice('00:00:00:00:00:01', 'aa:bb:cc:dd:ee:ff')
            False  # H1 (upper) e MAC sconosciuto -> bloccato
        """
        slice1 = self._get_slice(mac1)
        slice2 = self._get_slice(mac2)

        # Se uno dei due MAC non e riconosciuto (None), blocca per sicurezza.
        # Questo approccio "fail-closed" garantisce che traffico imprevisto
        # non possa attraversare la rete senza autorizzazione.
        if slice1 is None or slice2 is None:
            return False

        return slice1 == slice2

    def _add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        """
        Installa una regola (flow entry) nella flow table dello switch.

        Una flow entry e composta da:
        - Match: i criteri che un pacchetto deve soddisfare (es. MAC src/dst)
        - Priority: determina l'ordine di valutazione (piu alta = prima)
        - Actions: le operazioni da applicare al pacchetto (es. output su porta)
        - Timeout: dopo quanto tempo di inattivita la regola viene rimossa

        Le regole installate determinano come lo switch gestira autonomamente
        i pacchetti futuri che corrispondono al match, senza dover consultare
        il controller. Questo riduce drasticamente la latenza per i pacchetti
        successivi al primo di ogni flusso.

        Args:
            datapath: Oggetto datapath che rappresenta la connessione con lo switch.
                     Contiene le informazioni sullo switch (dpid, porte, etc.)
                     e i metodi per inviare messaggi OpenFlow.
            priority (int): Priorita della regola, da 0 a 65535.
                           Una priorita piu alta corrisponde a una precedenza
                           maggiore nel matching. In caso di ambiguita (due regole
                           che matchano lo stesso pacchetto), vince quella con
                           priorita piu alta.
            match: Oggetto OFPMatch che specifica i criteri di corrispondenza.
                  Puo includere campi come eth_src, eth_dst, eth_type, in_port, etc.
                  Un match vuoto (OFPMatch()) corrisponde a tutti i pacchetti.
            actions (list): Lista di azioni OpenFlow da eseguire sui pacchetti.
                          Tipicamente contiene OFPActionOutput(porta) per l'inoltro.
                          Una lista vuota ([]) significa drop (scartare il pacchetto).
            idle_timeout (int): Secondi di inattivita dopo i quali la regola viene
                               rimossa automaticamente dallo switch. Il valore 0
                               significa che la regola non scade mai (permanente).

        Note:
            Le regole sono identificate dalla coppia (match, priority). Se esiste
            gia una regola con gli stessi criteri e la stessa priorita, viene
            sovrascritta silenziosamente dal nuovo OFPFlowMod.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Costruisci l'istruzione che applica le azioni al pacchetto.
        # OFPIT_APPLY_ACTIONS indica che le azioni devono essere eseguite
        # immediatamente (in contrasto con OFPIT_WRITE_ACTIONS che le
        # accumula per l'esecuzione alla fine della pipeline).
        instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]

        # Costruisci il messaggio OFPFlowMod per installare la regola.
        # OFPFlowMod e il messaggio standard OpenFlow per aggiungere,
        # modificare o rimuovere regole dalla flow table dello switch.
        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout
        )

        # Invia il messaggio allo switch attraverso la connessione OpenFlow.
        # L'invio e asincrono: il metodo ritorna immediatamente senza
        # attendere conferma dallo switch.
        datapath.send_msg(flow_mod)

    def _send_packet(self, datapath, msg, actions):
        """
        Invia un pacchetto attraverso le porte specificate (PacketOut).

        Questo metodo viene utilizzato per due scopi:
        1. Inoltrare il pacchetto corrente che ha triggerato il PacketIn,
           dopo aver installato la flow rule per i pacchetti futuri
        2. Propagare pacchetti broadcast sulle porte dello slice

        Il metodo gestisce due casi:
        - Pacchetto bufferizzato: lo switch ha mantenuto il pacchetto nel suo
          buffer interno. Basta inviare il buffer_id e lo switch sa quale
          pacchetto inoltrare. Piu efficiente (nessun dato nel messaggio).
        - Pacchetto non bufferizzato: il pacchetto completo e stato incluso
          nel messaggio PacketIn. Dobbiamo reinviarlo al completo nel
          messaggio PacketOut.

        Args:
            datapath: Oggetto datapath che rappresenta la connessione con lo switch
            msg: Messaggio PacketIn originale che ha triggerato l'elaborazione.
                 Contiene il buffer_id, la porta di ingresso e (opzionalmente)
                 i dati del pacchetto.
            actions (list): Lista di azioni OpenFlow da applicare al pacchetto.
                          Tipicamente una o piu OFPActionOutput(porta).

        Note:
            Il messaggio PacketOut viene inviato con la porta di ingresso
            originale del pacchetto (msg.match['in_port']), necessaria per
            il corretto funzionamento del pipeline OpenFlow.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Controlla se il pacchetto e stato bufferizzato nello switch.
        # Quando lo switch invia un PacketIn, puo scegliere di mantenere
        # il pacchetto originale nel suo buffer (assegnandogli un buffer_id)
        # oppure di includerlo nel messaggio. Il comportamento dipende dalla
        # configurazione e dalla capacita del buffer.
        if msg.buffer_id != ofproto.OFP_NO_BUFFER:
            # Caso 1: pacchetto bufferizzato nello switch.
            # Basta specificare il buffer_id e lo switch sa quale pacchetto
            # inoltrare. Non serve includere i dati nel messaggio.
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=msg.match['in_port'],
                actions=actions
            )
        else:
            # Caso 2: pacchetto NON bufferizzato.
            # Il pacchetto completo e stato incluso nel campo 'data' del
            # messaggio PacketIn. Dobbiamo reinviarlo nel PacketOut.
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=msg.match['in_port'],
                actions=actions,
                data=msg.data
            )

        # Invia il messaggio PacketOut allo switch.
        # Lo switch eseguira le azioni specificate sul pacchetto.
        datapath.send_msg(out)

    # ==========================================================================
    # EVENT HANDLERS
    # ==========================================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Gestisce l'evento di connessione di un nuovo switch al controller.

        Questo handler viene invocato automaticamente dal framework Ryu quando
        uno switch completa l'handshake OpenFlow e comunica le sue funzionalita
        (Features Reply). E' il momento ideale per installare le regole di base
        che devono essere presenti su ogni switch fin dall'inizio.

        L'handler opera nello stato CONFIG_DISPATCHER, che indica che lo switch
        sta completando la configurazione iniziale. Durante questa fase, lo
        switch accetta solo messaggi di configurazione (come OFPFlowMod) e
        non genera ancora eventi PacketIn.

        Regole installate su ogni switch:
        1. Table-miss (priorita 0): invia al controller tutti i pacchetti che
           non corrispondono a nessuna altra regola. Questo garantisce che il
           controller possa prendere decisioni su ogni nuovo flusso.
        2. Drop IPv6 (priorita 5): scarta silenziosamente tutto il traffico
           IPv6 (EtherType 0x86DD). Il traffico IPv6 non e gestito in questo
           progetto e causerebbe carico inutile sul controller.

        Args:
            ev: Evento EventOFPSwitchFeatures contenente:
                - ev.msg.datapath: oggetto datapath dello switch connesso
                - ev.msg.datapath.id: DPID (identificatore univoco) dello switch
                - ev.msg.n_buffers: numero di buffer disponibili nello switch
                - ev.msg.n_tables: numero di flow table supportate

        Note:
            Questo handler viene invocato una volta per ogni switch che si
            connette al controller. Con la topologia a 6 switch, verra
            invocato 6 volte all'avvio della rete.
        """
        # Estrai le informazioni necessarie dall'evento
        datapath = ev.msg.datapath      # Oggetto che rappresenta lo switch
        ofproto = datapath.ofproto      # Costanti del protocollo OpenFlow
        parser = datapath.ofproto_parser  # Factory per messaggi OpenFlow
        dpid = datapath.id              # DPID (DataPath ID) dello switch

        self.logger.info("Switch S%d connesso - Installazione regole base", dpid)

        # ------------------------------------------------------------------
        # REGOLA 1: TABLE-MISS (priorita 0)
        # ------------------------------------------------------------------
        # La regola table-miss e la regola di default che cattura tutti i
        # pacchetti che non corrispondono a nessuna altra regola nella
        # flow table. Senza questa regola, i pacchetti non matchati
        # verrebbero scartati silenziosamente dallo switch (comportamento
        # predefinito di OpenFlow 1.3).
        #
        # Configurazione:
        # - Match: vuoto (OFPMatch()) = corrisponde a TUTTI i pacchetti
        # - Priorita: 0 = la piu bassa possibile (regola di ultima istanza)
        # - Azione: invia al controller (OFPP_CONTROLLER)
        # - Buffer: OFPCML_NO_BUFFER = invia l'intero pacchetto, non bufferizzare
        #
        # Il flag OFPCML_NO_BUFFER indica allo switch di inviare il pacchetto
        # completo al controller (non solo l'header). Questo e necessario
        # perche il controller deve poter analizzare e reinviare il pacchetto.

        match_table_miss = parser.OFPMatch()  # Match vuoto = wildcard totale
        actions_table_miss = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,      # Porta speciale: invia al controller
                ofproto.OFPCML_NO_BUFFER      # Non bufferizzare, invia tutto il pacchetto
            )
        ]
        self._add_flow(datapath, self.PRIORITY_TABLE_MISS,
                       match_table_miss, actions_table_miss)

        # ------------------------------------------------------------------
        # REGOLA 2: DROP IPv6 (priorita 5)
        # ------------------------------------------------------------------
        # Il traffico IPv6 non e gestito in questo progetto (la rete usa
        # esclusivamente IPv4 con indirizzi 10.0.0.x). Senza questa regola,
        # i pacchetti IPv6 generati automaticamente dal kernel Linux degli
        # host (Neighbor Discovery, Router Solicitation, etc.) verrebbero
        # inviati al controller tramite la regola table-miss, causando
        # carico inutile e messaggi di log superflui.
        #
        # Configurazione:
        # - Match: eth_type=0x86DD (EtherType per IPv6)
        # - Priorita: 5 (superiore alla table-miss, inferiore al forwarding)
        # - Azione: nessuna (lista vuota [] = DROP silenzioso)
        #
        # La priorita 5 garantisce che questa regola venga valutata PRIMA
        # della table-miss (priorita 0), ma DOPO le regole di forwarding
        # (priorita 10). In questo modo i pacchetti IPv6 non raggiungono
        # mai il controller.

        match_ipv6 = parser.OFPMatch(eth_type=0x86DD)  # Match su EtherType IPv6
        self._add_flow(datapath, self.PRIORITY_DROP_IPV6,
                       match_ipv6, [])  # Lista azioni vuota = DROP

        self.logger.info(
            "Switch S%d: regole base installate (table-miss priorita %d, "
            "drop IPv6 priorita %d)",
            dpid, self.PRIORITY_TABLE_MISS, self.PRIORITY_DROP_IPV6
        )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Gestisce i pacchetti inviati al controller dallo switch (PacketIn).

        Questo e l'handler principale che implementa tutta la logica di
        Topology Slicing. Viene invocato ogni volta che uno switch riceve
        un pacchetto che corrisponde alla regola table-miss (nessuna altra
        regola nella flow table lo matcha).

        L'handler opera nello stato MAIN_DISPATCHER, che indica che lo switch
        e completamente operativo e pronto per gestire traffico di rete.

        Logica di elaborazione (in ordine):
        1. Parsing dell'header Ethernet per estrarre MAC sorgente e destinazione
        2. Filtro LLDP: ignora i pacchetti di Link Layer Discovery Protocol
        3. Gestione broadcast (ARP): propaga SOLO all'interno dello slice
        4. Verifica isolamento slice per pacchetti unicast:
           a. Se sorgente e destinazione sono nello stesso slice -> installa
              regola di forwarding e inoltra il pacchetto
           b. Se sono in slice diverse -> drop silenzioso (blocco cross-slice)
           c. Se la destinazione non e nella tabella di routing -> drop con warning

        Args:
            ev: Evento EventOFPPacketIn contenente:
                - ev.msg: il messaggio PacketIn completo
                - ev.msg.datapath: lo switch che ha generato l'evento
                - ev.msg.match['in_port']: la porta di ingresso del pacchetto
                - ev.msg.data: i dati raw del pacchetto (se non bufferizzato)
                - ev.msg.buffer_id: ID del buffer nello switch (se bufferizzato)

        Note:
            Per ogni pacchetto unicast consentito, viene installata una flow rule
            con match su eth_src e eth_dst, che permette allo switch di gestire
            autonomamente i pacchetti futuri dello stesso flusso. Questo approccio
            reattivo introduce latenza solo per il primo pacchetto.
        """
        # ==================================================================
        # ESTRAZIONE INFORMAZIONI DAL MESSAGGIO PACKETIN
        # ==================================================================
        msg = ev.msg                       # Messaggio PacketIn completo
        datapath = msg.datapath            # Oggetto datapath dello switch
        ofproto = datapath.ofproto         # Costanti protocollo OpenFlow
        parser = datapath.ofproto_parser   # Factory per messaggi OpenFlow
        dpid = datapath.id                 # DPID dello switch (1-6)
        in_port = msg.match['in_port']     # Porta di ingresso del pacchetto

        # ==================================================================
        # PARSING DEL PACCHETTO
        # ==================================================================
        # Utilizziamo la libreria ryu.lib.packet per decodificare i vari
        # livelli del pacchetto di rete. Il parsing e incrementale:
        # prima l'header Ethernet, poi eventualmente ARP o IPv4.

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # Verifica che il pacchetto contenga un header Ethernet valido.
        # In condizioni normali questo e sempre vero, ma e buona pratica
        # verificarlo per robustezza.
        if eth is None:
            return

        src_mac = eth.src           # MAC address sorgente (es. '00:00:00:00:00:01')
        dst_mac = eth.dst           # MAC address destinazione
        eth_type = eth.ethertype    # Tipo di protocollo (0x0806=ARP, 0x0800=IPv4, etc.)

        # ==================================================================
        # FILTRO LLDP (Link Layer Discovery Protocol)
        # ==================================================================
        # LLDP (EtherType 0x88CC) e un protocollo usato per la scoperta
        # automatica della topologia di rete. In questo progetto non lo
        # utilizziamo (la topologia e definita staticamente in topology.py),
        # quindi ignoriamo silenziosamente questi pacchetti senza nemmeno
        # loggarli, per evitare di inquinare l'output.

        if eth_type == 0x88CC:
            return

        # Log dettagliato per il debugging (a livello DEBUG per non
        # riempire i log durante il funzionamento normale).
        # Mostra lo switch, la porta di ingresso, i MAC e il tipo di pacchetto.
        self.logger.debug(
            "S%d porta %d: %s -> %s (EtherType=0x%04x)",
            dpid, in_port, src_mac, dst_mac, eth_type
        )

        # ==================================================================
        # GESTIONE BROADCAST (ARP CONFINEMENT)
        # ==================================================================
        # I pacchetti con destinazione broadcast (ff:ff:ff:ff:ff:ff) sono
        # tipicamente ARP Request: un host vuole scoprire il MAC address
        # corrispondente a un indirizzo IP.
        #
        # In una rete senza slicing, il broadcast verrebbe propagato su
        # tutte le porte di tutti gli switch, raggiungendo TUTTI gli host.
        # Con il Topology Slicing, il broadcast deve essere CONFINATO
        # all'interno dello slice di appartenenza della sorgente.
        #
        # Esempio: se H1 (upper slice) invia un ARP "Who has 10.0.0.3?",
        # il broadcast deve raggiungere SOLO H3 (upper slice), e NON
        # H2 o H4 (lower slice).
        #
        # Questo viene gestito dal metodo _handle_broadcast().

        if dst_mac == 'ff:ff:ff:ff:ff:ff':
            self._handle_broadcast(datapath, in_port, src_mac, msg)
            return

        # ==================================================================
        # VERIFICA ISOLAMENTO SLICE (CROSS-SLICE CHECK)
        # ==================================================================
        # Prima di inoltrare qualsiasi pacchetto unicast, verifichiamo che
        # sorgente e destinazione appartengano allo stesso slice.
        #
        # Se non appartengono allo stesso slice, il pacchetto viene scartato
        # silenziosamente (drop). Non installiamo una regola di drop perche
        # il traffico cross-slice non dovrebbe nemmeno verificarsi in
        # condizioni normali (grazie all'ARP confinement, gli host di slice
        # diverse non possono scoprirsi reciprocamente).
        #
        # Il log a livello WARNING permette di tracciare eventuali tentativi
        # di comunicazione cross-slice, utile per il debugging.

        if not self._is_same_slice(src_mac, dst_mac):
            self.logger.warning(
                "S%d: BLOCCATO traffico cross-slice %s -> %s "
                "(slice %s -> slice %s)",
                dpid, src_mac, dst_mac,
                self._get_slice(src_mac), self._get_slice(dst_mac)
            )
            # Drop silenzioso: non facciamo nulla, il pacchetto viene scartato.
            # Non installiamo una regola di drop per evitare di consumare
            # spazio nella flow table per traffico che non dovrebbe esistere.
            return

        # ==================================================================
        # FORWARDING UNICAST INTRA-SLICE
        # ==================================================================
        # Se siamo arrivati qui, abbiamo verificato che sorgente e destinazione
        # appartengono allo stesso slice. Procediamo con l'inoltro del pacchetto.
        #
        # Il processo prevede due fasi:
        # 1. Installazione della flow rule: lo switch memorizzerà la regola
        #    e gestira autonomamente i pacchetti futuri dello stesso flusso
        # 2. Inoltro del pacchetto corrente: il primo pacchetto viene inoltrato
        #    esplicitamente tramite PacketOut

        if dpid in self.MAC_TO_PORT and dst_mac in self.MAC_TO_PORT[dpid]:
            # Trova la porta di uscita dalla tabella di routing statico.
            # La tabella MAC_TO_PORT e stata definita considerando SOLO i
            # percorsi standard (senza Premium Links).
            out_port = self.MAC_TO_PORT[dpid][dst_mac]

            # Costruisci il match per la flow rule.
            # Usiamo sia eth_src che eth_dst per garantire che la regola
            # sia specifica al flusso bidirezionale tra due host.
            # Questo evita ambiguita nel caso in cui lo stesso switch
            # gestisca traffico di entrambe le slice (come S1 e S6).
            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)

            # L'unica azione e l'output sulla porta di destinazione
            actions = [parser.OFPActionOutput(out_port)]

            # Installa la flow rule con idle_timeout.
            # La regola restera nella flow table fino a quando transita
            # traffico corrispondente, e verra rimossa dopo FLOW_IDLE_TIMEOUT
            # secondi di inattivita.
            self._add_flow(
                datapath,
                self.PRIORITY_FORWARDING,
                match,
                actions,
                idle_timeout=self.FLOW_IDLE_TIMEOUT
            )

            self.logger.info(
                "S%d: Regola installata %s -> %s via porta %d "
                "(idle_timeout=%ds)",
                dpid, src_mac, dst_mac, out_port, self.FLOW_IDLE_TIMEOUT
            )

            # Inoltra il pacchetto corrente (quello che ha triggerato il PacketIn).
            # I pacchetti futuri dello stesso flusso verranno gestiti
            # direttamente dalla flow rule appena installata.
            self._send_packet(datapath, msg, actions)

        else:
            # La destinazione non e presente nella tabella di routing per
            # questo switch. Questo puo accadere in due casi:
            # 1. Un MAC sconosciuto (host non previsto nella topologia)
            # 2. Un MAC di una slice diversa su uno switch che non lo gestisce
            #    (es. MAC di H2 su S2, che e dedicato all'upper slice)
            #
            # In entrambi i casi, il pacchetto viene scartato. Questo e il
            # comportamento corretto: gli switch intermedi (S2, S3, S4, S5)
            # conoscono solo i MAC della propria slice.
            self.logger.warning(
                "S%d: Destinazione %s non trovata nella tabella di routing "
                "(src=%s, in_port=%d) - pacchetto scartato",
                dpid, dst_mac, src_mac, in_port
            )

    def _handle_broadcast(self, datapath, in_port, src_mac, msg):
        """
        Gestisce i pacchetti broadcast confinandoli all'interno dello slice.

        Questo metodo implementa l'ARP Confinement, ovvero la limitazione della
        propagazione dei pacchetti broadcast (tipicamente ARP Request) alle sole
        porte appartenenti allo slice della sorgente.

        L'ARP Confinement e fondamentale per l'isolamento tra slice perche:
        - Senza confinamento, un ARP Request di H1 raggiungerebbe TUTTI gli host,
          inclusi H2 e H4 che appartengono al lower slice
        - Con il confinamento, l'ARP Request di H1 raggiunge SOLO H3
        - Questo impedisce agli host di slice diverse di scoprirsi reciprocamente
        - Di conseguenza, gli host non possono nemmeno tentare di comunicare
          cross-slice (non conoscono il MAC dell'host nell'altra slice)

        Algoritmo:
        1. Determina lo slice di appartenenza della sorgente (upper o lower)
        2. Cerca nella tabella SLICE_PORTS le porte di quello slice su questo switch
        3. Rimuovi la porta di ingresso dall'elenco (per evitare loop L2)
        4. Costruisci un'azione di output per ogni porta rimanente
        5. Inoltra il pacchetto broadcast sulle porte selezionate

        Esempio (Upper Slice, H1 invia ARP su S1):
            - src_mac = 00:00:00:00:00:01 (H1) -> slice = 'upper'
            - SLICE_PORTS[1]['upper'] = [1, 3] (H1 + link verso S2)
            - in_port = 1 (H1), quindi out_ports = [3] (solo link verso S2)
            - Il broadcast prosegue: S2 -> S3 -> S6 -> H3

        Esempio (Lower Slice, H4 invia ARP su S6):
            - src_mac = 00:00:00:00:00:04 (H4) -> slice = 'lower'
            - SLICE_PORTS[6]['lower'] = [4, 6] (link da S5 + H4)
            - in_port = 6 (H4), quindi out_ports = [4] (solo link verso S5)
            - Il broadcast prosegue: S5 -> S4 -> S1 -> H2

        Args:
            datapath: Oggetto datapath dello switch che ha ricevuto il broadcast
            in_port (int): Porta fisica da cui e arrivato il pacchetto broadcast
            src_mac (str): MAC address sorgente del pacchetto broadcast
            msg: Messaggio PacketIn originale (necessario per _send_packet)

        Note:
            Non vengono installate flow rule per i broadcast. Ogni pacchetto
            broadcast passa sempre dal controller. Questo e necessario perche
            il set di porte di uscita puo variare in base alla porta di ingresso
            (esclusione della porta sorgente), rendendo impossibile una singola
            regola statica.
        """
        dpid = datapath.id
        parser = datapath.ofproto_parser

        # ------------------------------------------------------------------
        # FASE 1: Determinazione dello slice della sorgente
        # ------------------------------------------------------------------
        # Identifichiamo a quale slice appartiene l'host che ha generato
        # il broadcast. Se il MAC non e riconosciuto, il broadcast viene
        # scartato per sicurezza.

        src_slice = self._get_slice(src_mac)

        if src_slice is None:
            # MAC sorgente non riconosciuto. Non dovrebbe accadere con host
            # configurati staticamente, ma gestiamo il caso per robustezza.
            self.logger.warning(
                "S%d: Broadcast da MAC sconosciuto %s su porta %d - scartato",
                dpid, src_mac, in_port
            )
            return

        # ------------------------------------------------------------------
        # FASE 2: Recupero delle porte dello slice su questo switch
        # ------------------------------------------------------------------
        # Dalla tabella SLICE_PORTS otteniamo la lista delle porte fisiche
        # che appartengono allo slice della sorgente su questo specifico switch.
        # Poi escludiamo la porta di ingresso per evitare di rimandare il
        # pacchetto da dove e arrivato (prevenzione loop L2).

        if dpid in self.SLICE_PORTS:
            # Ottieni le porte dello slice corrente
            slice_ports = self.SLICE_PORTS[dpid].get(src_slice, [])

            # Escludi la porta di ingresso dalla lista delle porte di uscita.
            # Se non la escludessimo, il pacchetto tornerebbe indietro verso
            # la sorgente, causando potenzialmente loop di broadcast.
            out_ports = [p for p in slice_ports if p != in_port]
        else:
            # Switch non presente nella tabella SLICE_PORTS.
            # Non dovrebbe accadere con la topologia a 6 switch configurata,
            # ma gestiamo il caso per robustezza.
            out_ports = []

        # ------------------------------------------------------------------
        # FASE 3: Verifica disponibilita porte di uscita
        # ------------------------------------------------------------------
        # Se non ci sono porte di uscita disponibili (perche lo switch
        # non ha porte dello slice della sorgente, oppure l'unica porta
        # era quella di ingresso), il broadcast termina qui.

        if not out_ports:
            self.logger.debug(
                "S%d: Broadcast %s da porta %d - nessuna porta di uscita "
                "per slice '%s'",
                dpid, src_mac, in_port, src_slice
            )
            return

        # ------------------------------------------------------------------
        # FASE 4: Costruzione delle azioni e inoltro
        # ------------------------------------------------------------------
        # Creiamo un'azione OFPActionOutput per ogni porta di uscita.
        # Il pacchetto verra duplicato e inviato su tutte le porte elencate.

        actions = [parser.OFPActionOutput(port) for port in out_ports]

        self.logger.debug(
            "S%d: Broadcast %s slice '%s': porta %d -> porte %s",
            dpid, src_mac, src_slice, in_port, out_ports
        )

        # Inoltra il pacchetto broadcast sulle porte selezionate
        self._send_packet(datapath, msg, actions)


# ==============================================================================
# NOTE SULL'IMPLEMENTAZIONE
# ==============================================================================
#
# APPROCCIO REATTIVO VS PROATTIVO
# --------------------------------
# Questo controller utilizza un approccio prevalentemente reattivo:
# - Le regole di forwarding vengono installate on-demand (al primo pacchetto)
# - Il primo pacchetto di ogni flusso passa attraverso il controller
# - I pacchetti successivi vengono gestiti direttamente dallo switch
# - Le regole scadono dopo 300 secondi di inattivita
#
# Un approccio proattivo (installazione di tutte le regole all'avvio)
# eliminerebbe la latenza del primo pacchetto, ma richiederebbe la
# conoscenza a priori di tutti i possibili flussi. Per il Topology Slicing,
# con soli 4 host e percorsi statici, il carico sul controller e trascurabile.
#
# PERCHE NON USARE I PREMIUM LINKS
# ---------------------------------
# Nel Topology Slicing, l'obiettivo e dimostrare l'isolamento fisico tra slice.
# Usare i percorsi standard (piu lenti, 2 Mbps con 50ms di latenza per hop)
# evidenzia chiaramente i limiti del semplice partizionamento topologico.
# I Premium Links verranno attivati nelle fasi successive:
# - Service Slicing: il traffico video usa il Premium Link, il resto usa lo standard
# - Dynamic Slicing: il Premium Link viene allocato dinamicamente in base al carico
#
# SICUREZZA E ISOLAMENTO
# ----------------------
# L'isolamento tra slice e garantito a tre livelli:
# 1. ARP Confinement: gli host di slice diverse non si scoprono reciprocamente
# 2. Cross-Slice Check: anche con MAC noti, il forwarding unicast e bloccato
# 3. Routing Table: gli switch intermedi non hanno rotte per MAC cross-slice
#
# Limitazioni note:
# - MAC spoofing potrebbe aggirare l'isolamento (mitigabile con port-security)
# - ARP spoofing potrebbe inquinare le cache (mitigabile con ARP inspection)
# - Per sicurezza enterprise, considerare 802.1X o MACsec
#
# SCALABILITA
# -----------
# Il numero massimo di regole installate e limitato dal prodotto:
#   (numero di flussi attivi) x (numero di switch nel percorso)
# Con 2 flussi bidirezionali (H1<->H3 e H2<->H4) e 4-5 switch per percorso,
# il totale massimo e circa 20 regole, ben entro i limiti di qualsiasi switch.
# L'idle_timeout garantisce che le regole inutilizzate vengano rimosse.
#
# ==============================================================================
