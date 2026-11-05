        ORG     $0000
        DC.L    $0000A000
        DC.L    $00000008
        LEA MSG(PC), A0
        BSR.W PUTS
        BRA.S *
PUTCHAR SWAP D0
WAIT_TX MOVE.B $80E001, D0
        AND.B #$02, D0
        BEQ.S WAIT_TX
        SWAP D0
        MOVE.B D0, $80E000
        RTS
PUTS MOVE.B (A0)+, D0
        BEQ.W PUTS_END
        BSR.S PUTCHAR
        BRA.S PUTS
PUTS_END RTS
MSG DC.B    $0D,$0A
        DC.B    "Hello MC"
        DC.B    "68008 Wo"
        DC.B    "rld!"
        DC.B    $0D,$0A,$0D,$0A
        DC.B    $00
        END
