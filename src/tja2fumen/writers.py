from tja2fumen.utils import write_struct
from tja2fumen.constants import BRANCH_NAMES, FUMEN_TYPE_NOTES


def write_fumen(path_out, song):
    with open(path_out, "wb") as file:
        file.write(song.header.raw_bytes)

        for measure_number in range(len(song.measures)):
            measure = song.measures[measure_number]
            measure_struct = ([measure.bpm, measure.offset_start,
                               int(measure.gogo), int(measure.barline),
                               measure.padding1] + measure.branch_info +
                              [measure.padding2])
            write_struct(file, song.header.order,
                         format_string="ffBBHiiiiiii",
                         value_list=measure_struct)

            for branch_number in range(len(BRANCH_NAMES)):
                branch = measure.branches[BRANCH_NAMES[branch_number]]
                branch_struct = [branch.length, branch.padding, branch.speed]
                write_struct(file, song.header.order,
                             format_string="HHf",
                             value_list=branch_struct)

                for note_number in range(branch.length):
                    note = branch.notes[note_number]
                    note_struct = [FUMEN_TYPE_NOTES[note.type], note.pos,
                                   note.item, note.padding]
                    if note.hits:
                        extra_vals = [note.hits, note.hits_padding]
                    else:
                        extra_vals = [note.score_init, note.score_diff * 4]
                    note_struct.extend(extra_vals + [note.duration])
                    write_struct(file, song.header.order,
                                 format_string="ififHHf",
                                 value_list=note_struct)

                    if note.type.lower() == "drumroll":
                        file.write(note.drumroll_bytes)
