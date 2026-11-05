        ORG     $0000
        DC.L    $00001000
        DC.L    $00000008
        LEA $4000, A0
        MOVE.B (A0), D0
        ADDQ.B #1, D0
        MOVE.B D0, (A0)
        JMP $0008
        END
