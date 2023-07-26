import os
import re
import struct
from copy import deepcopy

from tja2fumen.types import (TJASong, TJAMeasure, TJAData, FumenCourse,
                             FumenMeasure, FumenBranch, FumenNote, FumenHeader)
from tja2fumen.constants import (NORMALIZE_COURSE, TJA_NOTE_TYPES,
                                 BRANCH_NAMES, FUMEN_NOTE_TYPES)

###############################################################################
#                          TJA-parsing functions                              #
###############################################################################


def parse_tja(fname_tja):
    """Read in lines of a .tja file and load them into a TJASong object."""
    try:
        tja_text = open(fname_tja, "r", encoding="utf-8-sig").read()
    except UnicodeDecodeError:
        tja_text = open(fname_tja, "r", encoding="shift-jis").read()

    tja_lines = [line for line in tja_text.splitlines() if line.strip() != '']
    tja = split_tja_lines_into_courses(tja_lines)
    for course in tja.courses.values():
        parse_tja_course_data(course)

    return tja


def split_tja_lines_into_courses(lines):
    """
    Parse TJA metadata in order to divide TJA lines into separate courses.
    
    In TJA files, metadata lines are denoted by a colon (':'). These lines
    provide general info about the song (BPM, TITLE, OFFSET, etc.). They also
    define properties for each course in the song (difficulty, level, etc.).

    This function processes each line of metadata, and assigns the metadata
    to TJACourse objects (one for each course). To separate each course, this
    function uses the `COURSE:` metadata and any `#START P1/P2` commands,
    resulting in the following structure:

    TJASong
    ├─ TJACourse (e.g. Ura)
    │  ├─ Course metadata (level, balloons, scoreinit, scorediff, etc.)
    │  └─ Unparsed data (notes, commands)
    ├─ TJACourse (e.g. Ura-P1)
    ├─ TJACourse (e.g. Ura-P2)
    ├─ TJACourse (e.g. Oni)
    ├─ TJACourse (e.g. Hard)
    └─ ...

    The data for each TJACourse can then be parsed individually using the
    `parse_tja_course_data()` function.
    """
    # Strip leading/trailing whitespace and comments ('// Comment')
    lines = [line.split("//")[0].strip() for line in lines
             if line.split("//")[0].strip()]

    parsed_tja = None
    current_course = ''
    current_course_basename = ''
    song_bpm = 0
    song_offset = 0

    for line in lines:
        # Only metadata and #START commands are relevant for this function
        match_metadata = re.match(r"^([A-Z]+):(.*)", line)
        match_start = re.match(r"^#START(?:\s+(.+))?", line)

        # Case 1: Metadata lines
        if match_metadata:
            name_upper = match_metadata.group(1).upper()
            value = match_metadata.group(2).strip()

            # Global metadata fields
            if name_upper in ['BPM', 'OFFSET']:
                if name_upper == 'BPM':
                    song_bpm = value
                elif name_upper == 'OFFSET':
                    song_offset = value
                if song_bpm and song_offset:
                    parsed_tja = TJASong(song_bpm, song_offset)

            # Course-specific metadata fields
            elif name_upper == 'COURSE':
                if value not in NORMALIZE_COURSE.keys():
                    raise ValueError(f"Invalid COURSE value: '{value}'")
                current_course = NORMALIZE_COURSE[value]
                current_course_basename = current_course
            elif name_upper == 'LEVEL':
                if value not in ['1', '2', '3', '4', '5',
                                 '6', '7', '8', '9', '10']:
                    raise ValueError(f"Invalid LEVEL value: '{value}")
                parsed_tja.courses[current_course].level = int(value)
            elif name_upper == 'SCOREINIT':
                parsed_tja.courses[current_course].score_init = \
                    int(value.split(",")[-1]) if value else 0
            elif name_upper == 'SCOREDIFF':
                parsed_tja.courses[current_course].score_diff = \
                    int(value.split(",")[-1]) if value else 0
            elif name_upper == 'BALLOON':
                if value:
                    balloons = [int(v) for v in value.split(",") if v]
                    parsed_tja.courses[current_course].balloon = balloons
            elif name_upper == 'STYLE':
                # Reset the course name to remove "P1/P2" that may have been
                # added by a previous STYLE:DOUBLE chart
                if value == 'Single':
                    current_course = current_course_basename
            else:
                pass  # Ignore 'TITLE', 'SUBTITLE', 'WAVE', etc.

        # Case 2: #START commands
        elif match_start:
            value = match_start.group(1) if match_start.group(1) else ''
            # For STYLE:Double, #START P1/P2 indicates the start of a new
            # chart. But, we want multiplayer charts to inherit the
            # metadata from the course as a whole, so we deepcopy the
            # existing course for that difficulty.
            if value in ["P1", "P2"]:
                current_course = current_course_basename + value
                parsed_tja.courses[current_course] = \
                    deepcopy(parsed_tja.courses[current_course_basename])
                parsed_tja.courses[current_course].data = list()
            elif value:
                raise ValueError(f"Invalid value '{value}' for #START.")

            # Since P1/P2 has been handled, we can just use a normal '#START'
            parsed_tja.courses[current_course].data.append("#START")

        # Case 3: For other commands and data, simply copy as-is (parse later)
        else:
            parsed_tja.courses[current_course].data.append(line)

    # If a course has no song data, then this is likely because the course has
    # "STYLE: Double" but no "STYLE: Single". To fix this, we copy over the P1
    # chart from "STYLE: Double" to fill the "STYLE: Single" role.
    for course_name, course in parsed_tja.courses.items():
        if not course.data:
            if course_name+"P1" in parsed_tja.courses.keys():
                parsed_tja.courses[course_name] = \
                    deepcopy(parsed_tja.courses[course_name+"P1"])

    # Remove any charts (e.g. P1/P2) not present in the TJA file (empty data)
    for course_name in [k for k, v in parsed_tja.courses.items()
                        if not v.data]:
        del parsed_tja.courses[course_name]

    return parsed_tja


def parse_tja_course_data(course):
    """
    Parse course data (notes, commands) into a nested song structure.

    The goal of this function is to take raw note and command strings
    (e.g. '1020,', '#BPMCHANGE') and parse their values into appropriate
    types (e.g. lists, ints, floats, etc.).

    This function also processes measure separators (',') and branch commands
    ('#BRANCHSTART`, '#N`, '#E', '#M') to split the data into branches and
    measures, resulting in the following structure:

    TJACourse
    ├─ TJABranch ('normal')
    │  ├─ TJAMeasure
    │  │  ├─ TJAData (notes, commands)
    │  │  ├─ TJAData
    │  │  └─ ...
    │  ├─ TJAMeasure
    │  ├─ TJAMeasure
    │  └─ ...
    ├─ TJABranch ('professional')
    └─ TJABranch ('master')

    This provides a faithful, easy-to-inspect tree-style representation of the
    branches and measures within each course of the .tja file.
    """
    has_branches = bool([d for d in course.data if d.startswith('#BRANCH')])
    current_branch = 'all' if has_branches else 'normal'
    branch_condition = None
    flag_levelhold = False

    # Process course lines
    idx_m = 0
    idx_m_branchstart = 0
    for idx_l, line in enumerate(course.data):
        # 0. Check to see whether line is a command or note data
        command, value, notes = None, None, None
        match_command = re.match(r"^#([A-Z]+)(?:\s+(.+))?", line)
        if match_command:
            command, value = match_command.groups()
        else:
            notes = line  # If not a command, then line must be note data

        # 1. Parse measure notes
        if notes:
            # If measure has ended, then add notes to the current measure,
            # then start a new measure by incrementing idx_m
            if notes.endswith(','):
                for branch in (course.branches.keys()
                               if current_branch == 'all'
                               else [current_branch]):
                    course.branches[branch][idx_m].notes += notes[0:-1]
                    course.branches[branch].append(TJAMeasure())
                idx_m += 1
            # Otherwise, keep adding notes to the current measure ('idx_m')
            else:
                for branch in (course.branches.keys()
                               if current_branch == 'all'
                               else [current_branch]):
                    course.branches[branch][idx_m].notes += notes

        # 2. Parse measure commands that produce an "event"
        elif command in ['GOGOSTART', 'GOGOEND', 'BARLINEON', 'BARLINEOFF',
                         'DELAY', 'SCROLL', 'BPMCHANGE', 'MEASURE',
                         'SECTION', 'BRANCHSTART']:
            # Get position of the event
            for branch in (course.branches.keys() if current_branch == 'all'
                           else [current_branch]):
                pos = len(course.branches[branch][idx_m].notes)

            # Parse event type
            if command == 'GOGOSTART':
                current_event = TJAData('gogo', '1', pos)
            elif command == 'GOGOEND':
                current_event = TJAData('gogo', '0', pos)
            elif command == 'BARLINEON':
                current_event = TJAData('barline', '1', pos)
            elif command == 'BARLINEOFF':
                current_event = TJAData('barline', '0', pos)
            elif command == 'DELAY':
                current_event = TJAData('delay', float(value), pos)
            elif command == 'SCROLL':
                current_event = TJAData('scroll', float(value), pos)
            elif command == 'BPMCHANGE':
                current_event = TJAData('bpm', float(value), pos)
            elif command == 'MEASURE':
                current_event = TJAData('measure', value, pos)
            elif command == 'SECTION':
                # If #SECTION occurs before a #BRANCHSTART, then ensure that
                # it's present on every branch. Otherwise, #SECTION will only
                # be present on the current branch, and so the `branch_info`
                # values won't be correctly set for the other two branches.
                if course.data[idx_l+1].startswith('#BRANCHSTART'):
                    current_event = TJAData('section', None, pos)
                    current_branch = 'all'
                # Otherwise, #SECTION exists in isolation. In this case, to
                # reset the accuracy, we just repeat the previous #BRANCHSTART.
                else:
                    current_event = TJAData('branch_start', branch_condition,
                                            pos)
            elif command == 'BRANCHSTART':
                if flag_levelhold:
                    continue
                # Ensure that the #BRANCHSTART command is added to all branches
                current_branch = 'all'
                branch_condition = value.split(',')
                if branch_condition[0] == 'r':  # r = drumRoll
                    branch_condition[1] = int(branch_condition[1])  # drumrolls
                    branch_condition[2] = int(branch_condition[2])  # drumrolls
                elif branch_condition[0] == 'p':  # p = Percentage
                    branch_condition[1] = float(branch_condition[1]) / 100  # %
                    branch_condition[2] = float(branch_condition[2]) / 100  # %
                current_event = TJAData('branch_start', branch_condition, pos)
                # Preserve the index of the BRANCHSTART command to re-use
                idx_m_branchstart = idx_m

            # Append event to the current measure's events
            for branch in (course.branches.keys() if current_branch == 'all'
                           else [current_branch]):
                course.branches[branch][idx_m].events.append(current_event)

        # 3. Parse commands that don't create an event
        #    (e.g. simply changing the current branch)
        else:
            if command == 'START' or command == 'END':
                current_branch = 'all' if has_branches else 'normal'
                flag_levelhold = False
            elif command == 'LEVELHOLD':
                flag_levelhold = True
            elif command == 'N':
                current_branch = 'normal'
                idx_m = idx_m_branchstart
            elif command == 'E':
                current_branch = 'professional'
                idx_m = idx_m_branchstart
            elif command == 'M':
                current_branch = 'master'
                idx_m = idx_m_branchstart
            elif command == 'BRANCHEND':
                current_branch = 'all'

            else:
                print(f"Ignoring unsupported command '{command}'")

    # Delete the last measure in the branch if no notes or events
    # were added to it (due to preallocating empty measures)
    for branch in course.branches.values():
        if not branch[-1].notes and not branch[-1].events:
            del branch[-1]

    # Merge measure data and measure events in chronological order
    for branch_name, branch in course.branches.items():
        for measure in branch:
            notes = [TJAData('note', TJA_NOTE_TYPES[note], i)
                     for i, note in enumerate(measure.notes) if
                     TJA_NOTE_TYPES[note] != 'Blank']
            events = measure.events
            while notes or events:
                if events and notes:
                    if notes[0].pos >= events[0].pos:
                        measure.combined.append(events.pop(0))
                    else:
                        measure.combined.append(notes.pop(0))
                elif events:
                    measure.combined.append(events.pop(0))
                elif notes:
                    measure.combined.append(notes.pop(0))

    # Ensure all branches have the same number of measures
    if has_branches:
        if len(set([len(b) for b in course.branches.values()])) != 1:
            raise ValueError(
                "Branches do not have the same number of measures. (This "
                "check was performed prior to splitting up the measures due "
                "to mid-measure commands. Please check the number of ',' you"
                "have in each branch.)"
            )


###############################################################################
#                          Fumen-parsing functions                            #
###############################################################################

def parse_fumen(fumen_file, exclude_empty_measures=False):
    """
    Parse bytes of a fumen .bin file into nested measures, branches, and notes.
    """
    file = open(fumen_file, "rb")
    size = os.fstat(file.fileno()).st_size

    song = FumenCourse(
        header=FumenHeader(raw_bytes=file.read(520))
    )

    for measure_number in range(song.header.b512_b515_number_of_measures):
        # Parse the measure data using the following `format_string`:
        #   "ffBBHiiiiiii" (12 format characters, 40 bytes per measure)
        #     - 'f': BPM               (one float (4 bytes))
        #     - 'f': fumenOffset       (one float (4 bytes))
        #     - 'B': gogo              (one unsigned char (1 byte))
        #     - 'B': barline           (one unsigned char (1 byte))
        #     - 'H': <padding>         (one unsigned short (2 bytes))
        #     - 'iiiiii': branch_info  (six integers (24 bytes))
        #     - 'i': <padding>         (one integer (4 bytes)
        measure_struct = read_struct(file, song.header.order,
                                     format_string="ffBBHiiiiiii")

        # Create the measure dictionary using the newly-parsed measure data
        measure = FumenMeasure(
            bpm=measure_struct[0],
            offset_start=measure_struct[1],
            gogo=measure_struct[2],
            barline=measure_struct[3],
            padding1=measure_struct[4],
            branch_info=list(measure_struct[5:11]),
            padding2=measure_struct[11]
        )

        # Iterate through the three branch types
        for branch_name in BRANCH_NAMES:
            # Parse the measure data using the following `format_string`:
            #   "HHf" (3 format characters, 8 bytes per branch)
            #     - 'H': total_notes ( one unsigned short (2 bytes))
            #     - 'H': <padding>  ( one unsigned short (2 bytes))
            #     - 'f': speed      ( one float (4 bytes)
            branch_struct = read_struct(file, song.header.order,
                                        format_string="HHf")

            # Create the branch dictionary using the newly-parsed branch data
            total_notes = branch_struct[0]
            branch = FumenBranch(
                length=total_notes,
                padding=branch_struct[1],
                speed=branch_struct[2],
            )

            # Iterate through each note in the measure (per branch)
            for note_number in range(total_notes):
                # Parse the note data using the following `format_string`:
                #   "ififHHf" (7 format characters, 24 bytes per note cluster)
                #     - 'i': note type
                #     - 'f': note position
                #     - 'i': item
                #     - 'f': <padding>
                #     - 'H': score_init
                #     - 'H': score_diff
                #     - 'f': duration
                # NB: 'item' doesn't seem to be used at all in this function.
                note_struct = read_struct(file, song.header.order,
                                          format_string="ififHHf")

                # Create the note dictionary using the newly-parsed note data
                note_type = note_struct[0]
                note = FumenNote(
                    note_type=FUMEN_NOTE_TYPES[note_type],
                    pos=note_struct[1],
                    item=note_struct[2],
                    padding=note_struct[3],
                )

                if note_type == 0xa or note_type == 0xc:
                    # Balloon hits
                    note.hits = note_struct[4]
                    note.hits_padding = note_struct[5]
                else:
                    song.score_init = note.score_init = note_struct[4]
                    song.score_diff = note.score_diff = note_struct[5] // 4

                # Drumroll/balloon duration
                note.duration = note_struct[6]

                # Account for padding at the end of drumrolls
                if note_type == 0x6 or note_type == 0x9 or note_type == 0x62:
                    note.drumroll_bytes = file.read(8)

                # Assign the note to the branch
                branch.notes.append(note)

            # Assign the branch to the measure
            measure.branches[branch_name] = branch

        # Assign the measure to the song
        song.measures.append(measure)
        if file.tell() >= size:
            break

    file.close()

    # NB: Official fumens often include empty measures as a way of inserting
    # barlines for visual effect. But, TJA authors tend not to add these empty
    # measures, because even without them, the song plays correctly. So, in
    # tests, if we want to only compare the timing of the non-empty measures
    # between an official fumen and a converted non-official TJA, then it's
    # useful to exclude the empty measures.
    if exclude_empty_measures:
        song.measures = [m for m in song.measures
                         if m.branches['normal'].length
                         or m.branches['professional'].length
                         or m.branches['master'].length]

    return song


def read_struct(file, order, format_string, seek=None):
    """
    Interpret bytes as packed binary data.

    Arguments:
        - file: The fumen's file object (presumably in 'rb' mode).
        - order: '<' or '>' (little or big endian).
        - format_string: String made up of format characters that describes
                         the data layout. Full list of available characters:
          (https://docs.python.org/3/library/struct.html#format-characters)
        - seek: The position of the read pointer to be used within the file.

    Return values:
        - interpreted_string: A string containing interpreted byte values,
                              based on the specified 'fmt' format characters.
    """
    if seek:
        file.seek(seek)
    expected_size = struct.calcsize(order + format_string)
    byte_string = file.read(expected_size)
    # One "official" fumen (AC11\deo\deo_n.bin) runs out of data early
    # This workaround fixes the issue by appending 0's to get the size to match
    if len(byte_string) != expected_size:
        byte_string += (b'\x00' * (expected_size - len(byte_string)))
    interpreted_string = struct.unpack(order + format_string, byte_string)
    return interpreted_string
