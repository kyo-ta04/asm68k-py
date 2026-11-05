        ORG     $0000
        DC.L    $0000A000
        DC.L    $00000008
        MOVE.B $80E001, D0
        ANDI.B #1, D0
        BEQ.S $000008
        MOVE.B $80E000, D0
        MOVE.B $80E001, D1
        ANDI.B #2, D1
        BEQ.S $001A
        MOVE.B D0, $80E000
        BRA.S $0008
        END
