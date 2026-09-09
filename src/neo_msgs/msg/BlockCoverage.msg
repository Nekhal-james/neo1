# What has been surveyed in one campus block. An element of KbCoverage.
#
# This exists so Neo can distinguish "that room isn't in my directory" from "I
# haven't learned that block yet" (CLAUDE.md). Those are different sentences to
# say to a student, and blurring them is how a receptionist ends up sounding
# either broken or dishonest.

string block                 # "A", "B", ...

int32[] floors               # floors with data; empty when NOT_SURVEYED

uint8 NOT_SURVEYED = 0       # nothing known -- "I haven't learned C block yet"
uint8 PARTIAL = 1            # some floors done; a miss here is genuinely ambiguous
uint8 COMPLETE = 2           # surveyed -- a miss here means the room does not exist
uint8 status

uint32 room_count
