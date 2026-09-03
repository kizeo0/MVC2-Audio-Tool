# ==============================================================================================
# Basic tool to dump content information and/or samples out of a DTPK file,
# as well splicing in compatible PCM AIFF, WAV files or YADPCM chunks.
# This tool is strongly based upon the previous research and work put together by
# kingshriek, Vincent, the Shenmue team (especially Sappharad and LemonHaze), and many others.
# All wisdom is from them, all mistakes are from me.
# Clarifications always appreciated.

# In the interest of clear nomenclature:
# The Sequencer chunk contains Groups of Track definitions.
# Sequencer Group Tracks in turn reference SONG data, DTPK events, and/or Sound Effect data.
# Sound Effect data is incorporated via reference to Sample Playback definitions (SPDs).
# Sample Playback definitions reference the sample ID to play back as well as how to play it
# back.  Notably playback rate/frequency is stored here.  It is useful to separate that from the
# actual sample data so that you can use the same SFX at different frequencies to represent
# different levels of panic/etc.
# The Sample bank contains the minimal information necessary to decipher the waveform data.
# This is type (YADPCM 4bit, PCM 8bit, PCM 16bit), number of channels (mono or stereo), and
# other immutable data.
# The waveform data is only slightly interesting in that the channels are stored linearly,
# as opposed to interleaved.

# Note that you should expect padding to 4 byte boundaries at the end of every discrete chunk.

# Update history located near end of file.

# TODO list:
# * bugbug/todos as noted in the code
# * figure out the correct interpretation of Powerstone 2-DC's BOSS2.BIN odd tracks
# * maybe allow type 0 MIDI import?  lossy tho
# * maybe allow user to opt into using stock SPD values instead of inherited?
# * encode to ADPCM?
# * add rate change for samples reused at different rates (such as mvc2 Tron Bonne)?
# * add delete option?
# * add joined-sample option?
# * add resampling to help with size concerns?
# * add volume change?

# As regards licensing, licensing herein is based upon the incorporated code from others who in
# turn seem to have license preferences.  My chief concern and interest is in respecting and
# celebrating the work and contributions of others.

# MIT License

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# ==============================================================================================

# ==============================================================================================
import os
import sys

# Note that the AIFC lib may be going away in Python 3.13: let's ignore the deprecation warning for now
# We can adjust for the new world order when/if it arrives.
import warnings ; warnings.warn = lambda *args,**kwargs: None
import aifc

import wave
import math
import zlib
from struct import pack
from struct import unpack
from array import array
from enum import Enum
# ==============================================================================================

# ==============================================================================================

# Config options since there's a lot of data
show_detailed_file_info = False
show_header_info = False
show_data_tracks = False
show_lost_tracks = False
show_detailed_combination_data = False
show_program_defs_data = False
show_unknown_chunk_data = False
show_detailed_data_tracks = False
show_detailed_data_playback = False
show_detailed_data_effects = False
show_detailed_data_samples = False
show_detailed_data_samples_full = False
dump_adpcm_samples = False
dump_pcm_samples_to_aiff = False
dump_pcm_samples_to_wav = False
dump_adpcm_samples_to_aiff = False
dump_adpcm_samples_to_wav = False
reassemble_mode = False
fVerboseOutput = False
# FWIW, 6 * 156000 = 936000.  The MvC2 common match effects file (SE_COMN.BIN) is 931284.
# They worked hard to fit within the 2MB audio limit for Dreamcast, and if you're
# injecting new samples you would want to adhere to that same minimalist approach.
mvc2_DTPK_size_limit = 156000

INVALID_SAMPLE_ID = 0xFFFF

# ==============================================================================================
# I'm unclear where the bcolors implementation sources from: it's referenced around the internet.
# Usage is as shown: make sure to close the presentation shift with ENDC.

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

warningCount = 0
def PrintAsWarning(msg):
	print (bcolors.WARNING + msg + bcolors.ENDC)
	global warningCount
	warningCount += 1

errorCount = 0
def PrintAsFailure(msg):
	print (bcolors.FAIL + msg + bcolors.ENDC)
	global errorCount
	errorCount += 1

def PrintValueIfWarranted(strOutput, valDefault, valChecking):
	if fVerboseOutput or (valDefault != valChecking):
		print (strOutput)

# ==============================================================================================
def DelayValuesTo44100SampleCount(unMaskedByte1, unMaskedByte2):
	# For a discussion of what these magic values are, see where we ingest the track composition data
	# Byte 1:
		# starts masked with 0x80
		# each odd +0x01 step is +0n5120 samples
		# each even +0x01 step is +0n6144 samples
		# a full 0x10 steps is +0n90112 samples (which is (8 * 5120) + (8 * 6144) conveniently enough)
	# Byte 2 is optional, and increments at chunks of 1024 samples every 0x17 steps
	delayInSamples = (int(math.floor(unMaskedByte1 / 2)) * 11264) + ((unMaskedByte1 % 2) * 5120)
	delayInSamples += int(math.floor(unMaskedByte2 / 23)) * 1024
	return delayInSamples

# ==============================================================================================
def UpdateDTPKChunkOffsets(dtpk_bytes, newOffsetUnknown3Chunk, newOffsetSequencerChunk, newOffsetPlaybackChunk, newOffsetEffectsChunk, newOffsetSampleChunk, fIsProbablyMvC2CharacterFile):
	dtpk_bytes[0x28:0x2c] = array('B', newOffsetUnknown3Chunk.to_bytes(4, byteorder='little'))
	dtpk_bytes[0x2c:0x30] = array('B', newOffsetSequencerChunk.to_bytes(4, byteorder='little'))
	dtpk_bytes[0x30:0x34] = array('B', newOffsetPlaybackChunk.to_bytes(4, byteorder='little'))
	dtpk_bytes[0x38:0x3c] = array('B', newOffsetEffectsChunk.to_bytes(4, byteorder='little'))
	dtpk_bytes[0x3c:0x40] = array('B', newOffsetSampleChunk.to_bytes(4, byteorder='little'))

	nActualFileSize = len(dtpk_bytes)
	nListedFileSize = unpack('<I', dtpk_bytes[0x08:0x0c])[0]

	# Update the filesize as necessary
	if (nActualFileSize != nListedFileSize):
		# Enforce alignment
		remnant = nActualFileSize % 4
		if (remnant != 0):
			print ('\tAdding %u bytes padding (size was 0x%x)' % (remnant, nActualFileSize))
			dtpk_bytes[nActualFileSize:nActualFileSize + remnant] = array('B', bytes(remnant))
			nActualFileSize += remnant
		print ('\tUpdating header: new file size 0x%x (was 0x%x)' % (nActualFileSize, nListedFileSize))
		dtpk_bytes[0x08:0x0c] = array('B', nActualFileSize.to_bytes(4, byteorder='little'))

	if ((nActualFileSize > mvc2_DTPK_size_limit) and fIsProbablyMvC2CharacterFile):
		PrintAsFailure('ERROR!  You have 2MB total for audio for Dreamcast.')
		print ('ERROR!  Keep file sizes for mvc2 characters to ~0x%x (%u) bytes to avoid overflow.' % (mvc2_DTPK_size_limit, mvc2_DTPK_size_limit))
		print ('ERROR!  If you cross that boundary, weird things will happen with sounds and/or you\'ll crash the Dreamcast.')
		print ('  If you\'re not worried about MvC2, you\'re probably fine.')
	elif (nActualFileSize > (nListedFileSize + 20000)):
		PrintAsWarning('Warning: These changes have grown the size of this audio file by %u bytes.\nThe Dreamcast has extremely limited memory.\n'
						'As such, downsampling injected files is usually smart if you want to avoid issues.'  % (nActualFileSize - nListedFileSize) )

# ==============================================================================================
class DTPKTrackEntry:
	# A normal track entry looks like:
	# C0 DF 01 78 80 FF
	# In order:
	# Track_Flags (C0) Playback_Type (DF) SPD_Used (01) Volume (78) End_Sample_Ref? (80) End_Track (FF)
	# Note: Maximum value of volume is 0x7f
	# For a Joined track such as Track 01 in SND_OPEN.BIN for cvs2:
	# C0 DF 01 78 00 8B 4E DF 02 78 80 FF
	# In order:
	# Track_Flags (C0) Playback_Type (DF) SPD_Used (01) Volume (78) Join_With (00) Delay_Part1 (0b | 80) Opt_Delay_Part2 (4e)
	#    Playback_Type (DF) SPD_Used (02) Volume (78) End_Sample_Ref (80) End_Track (FF)
	# The Delay value is relative to 0:00, not the end of the previous sample.

	def __init__(self, entry_type, SPD_used_or_event_id, volume_or_event_value, trailing_bytes):
		self.entry_type = entry_type
		self.SPD_used_or_event_id = SPD_used_or_event_id
		self.volume_or_event_value = volume_or_event_value
		self.trailing_bytes = trailing_bytes

	def SetSPDHighBits(self, SPD_high_bits):
		self.SPD_used_or_event_id += SPD_high_bits

	def Print(self):
		if IsTrackEntry(self.entry_type):
			summary = '\t\t\tType: SFX (0x%02x).  SPD: 0x%02x.  Volume: 0x%02x.' % (self.entry_type, self.SPD_used_or_event_id, self.volume_or_event_value)

			extraBytes = ""

			# Track Entry errata:
			# Cammy-CVS2 (SND_PL0C) track 36 "joins" two non-existent samples (25, 26) using terminal byte(?) 80
			# not a join/append byte (00/01).  But it's an unused track, so the composition is irrelevant.
			if self.trailing_bytes[0] & 0x80:
				extraBytes = ' Terminal byte: 0x%02x' % self.trailing_bytes[0]
			else:
				extraBytes = " Join bytes:"
				for val in self.trailing_bytes:
					extraBytes += " 0x%02x" % val

				if self.trailing_bytes[0] == 0:
					extraBytes += '\n\t\t\t\t    Meaning: Join Next (0x%02x)' % self.trailing_bytes[0]
				else:
					extraBytes += '\n\t\t\t\t    Meaning: Replace With? (0x%02x)' % self.trailing_bytes[0]

				delayInSecs = 0
				if len(self.trailing_bytes) > 1:
					unMaskedByte1 = self.trailing_bytes[1] ^ 0x80
					unMaskedByte2 = 0
					extraBytes += ' Delay Next: 0x%02x' % unMaskedByte1
					if len(self.trailing_bytes) > 2:
						unMaskedByte2 = self.trailing_bytes[2]
						extraBytes += ' 0x%02x' % self.trailing_bytes[2]
					delayInSamples = DelayValuesTo44100SampleCount(unMaskedByte1, unMaskedByte2)
					extraBytes += ' (~%1.3fs)' % (delayInSamples / 44100) # I did all my math at 44100
				if len(self.trailing_bytes) > 3:
					PrintAsFailure('ERROR: Unknown track entry data {}'.format(self.trailing_bytes))

			summary += extraBytes
		elif IsKnownTrackAction(self.entry_type):
			summary = '\t\t\tEvent: '
			
			if self.entry_type == 0xa0:
				summary += GetStringForSystemEventType(self.entry_type, self.SPD_used_or_event_id, self.volume_or_event_value)

			elif self.entry_type == 0xa4:
				summary += 'FX Channel (0x%02x).  ' % self.entry_type
				if self.SPD_used_or_event_id == 0x10:
					summary += 'Relevel (0x%02x).  ' % self.SPD_used_or_event_id
				elif self.SPD_used_or_event_id == 0x20:
					summary += 'Pan (0x%02x).  ' % self.SPD_used_or_event_id
				# Values are masked with 0x80 on disk
				summary += 'Value: 0x%02x (as 0x%02x)' % (self.volume_or_event_value ^ 0x80, self.volume_or_event_value)
			elif self.entry_type == 0xa9:
				summary += 'Request Bank Entry (0x%02x).  ' % self.entry_type
				summary += 'Use Entry: 0x%02x.  ' % self.SPD_used_or_event_id
				# Values are masked with 0x80 on disk
				summary += 'Unknown: 0x%02x (as 0x%02x)' % (self.volume_or_event_value ^ 0x80, self.volume_or_event_value)
		else:
			summary = '\t\t' + bcolors.FAIL + 'ERROR: Unknown type: 0x%02x.  Sample or Event: 0x%02x.  Data: 0x%02x.  Terminal byte: 0x%02x' % (self.entry_type, self.SPD_used_or_event_id, self.volume_or_event_value, self.trailing_bytes[0]) + bcolors.ENDC
		print (summary)

# DTPKTrack entries: 5-8 bytes per track.
	# ~5 bytes per C0 A0 entry: Event entry
	# ~6 bytes per C0 DF entry: Track entry
	# First byte indicates audio ouput ORd with sequencer group
	# Second byte:
	# 0xa? entries indicate Events
	# 0xd? entries indicate Tracks
	# Penultimate byte is 0x80 + the any overflow bits above SPD reference value 0x7f (so a reference to SPD 0x93 would end with 0x81 not 0x80)
	# 0xFF is always the terminal byte
	# This table can be used to join samples together!
	# For joins, based upon ~350 files, there is either two or three join bytes.
	# Consider AA BB [CC].
	# AA is wholly unknown but is either 00 (most often) or 01 (rarely)
	# BB appears to be 80 OR'd with a delay duration in 0.25s increments(?).  maybe in samples? unclear
	# CC is often present and seems to increase the delay duration
	# OK, spent some time generating variant DTPKs with different join delay values, and the values appear to be:
	# BB ^ 80 = variable length step.
		# each odd step is +0n5120 samples
		# each even step is +0n6144 samples
		# a full 0x10 steps is +0n90112 samples.
	# CC = floor(CC/0n23) * 0n1024 samples, so about 0.023s per increment
	# That's all speculative based upon an analysis of about 90 variant delays from 8000 (play at same time) to b96e, done with
	# both mvc2-dc and cvs2-dc files.
	# Somewhere between 0xb96e to 0xc96e we ... overflow(?) and the second/joined clip just doesn't play.

class DTPKTrack:
	def __init__(self, track_id, playback_id):
		# This is the track index within this specific group
		self.track_id = track_id
		# This is the track index within all groups
		self.playback_id = playback_id
		self.entryList = []
		self.starting_offset = 0
		self.raw_bytes = []
		self.bank_label = 0xA9

	def AddRawEntryData(self, bank_type, start_location, byte_array):
		read_offset = 0
		flags = (byte_array[read_offset:read_offset + 1])[0]
		self.sequencer_group = flags & 0x1F
		self.destination = flags & 0xF0
		self.sync_group = 0
		self.sync_level = 0

		read_offset += 1
		nTotalSPDsUsed = 0
		entry_type = (byte_array[read_offset:read_offset + 1])[0]

		if entry_type <= 0x7f:
			# Entry is using the optional SYNC and LEVEL values.  Each is value 00-0x7f.
			self.sync_group = entry_type
			read_offset += 1
			self.sync_level = (byte_array[read_offset:read_offset + 1])[0]
			read_offset += 1
			entry_type = (byte_array[read_offset:read_offset + 1])[0]

		self.raw_bytes = byte_array
		self.bank_label = bank_type

		if self.bank_label == 0xA8: # if SONG
			self.starting_offset = start_location
		else:
			if IsTrackEntry(entry_type):
				cur_byte = 0
				while IsTrackEntry(entry_type):
					trailing_bytes = []
					read_offset += 1
					# base sample ID to use
					# note that the value is 00-0x7f: integer overflow is stored for the entire SPD in the terminal byte's low 7.
					# So sample 0x80 would be [... 00 ... x1], sample 0x82 would be [... 00 ... x2], and so forth
					# This is slightly more interesting because of tracks joined with multiple SPDs that increment high bits
					# mid-track.  Use N-1 for those of course..
					SPD_to_use = (byte_array[read_offset:read_offset+1])[0]
					read_offset += 1

					# Note that there can be a conflict(?) between the Track Composition Data sample value and 
					# the Track Playback Data sample value.  For at least flycast, the non-zero value of the two is ignored.
					# In the instance where both are non-zero, the Track Composition Data value is used.
					volume_maybe = (byte_array[read_offset:read_offset+1])[0]
					read_offset += 1
						
					while (((byte_array[read_offset:read_offset+1])[0] != 0xa0) and
							(not (IsTrackEntry((byte_array[read_offset:read_offset+1])[0]))) and 
							((byte_array[read_offset:read_offset+1])[0] != 0xff)):
						# optional bytes
						cur_byte = (byte_array[read_offset:read_offset+1])[0]
						# first byte in the segment appears to be volume
						trailing_bytes.append(cur_byte)
						read_offset += 1
						# closure or join byte is 0xff

					self.AddSPDUsage(start_location, entry_type, SPD_to_use, volume_maybe, trailing_bytes)
					entry_type = (byte_array[read_offset:read_offset+1])[0]

				if len(trailing_bytes) == 1:
					previous_spd_lowbits = 0x7f
					traverses_bit_boundary = False
					
					for entry in self.entryList[::-1]:
						new_spd_lowbits = entry.SPD_used_or_event_id
						high_bits = (cur_byte & 0x0f)
						if new_spd_lowbits > previous_spd_lowbits:
							traverses_bit_boundary = True
						if traverses_bit_boundary and high_bits > 0:
							high_bits -= 1
						entry.SetSPDHighBits(high_bits * 0x80)
								
						previous_spd_lowbits = new_spd_lowbits
				else:
					PrintAsFailure('ERROR: Unknown SPD structure at 0x%x: should just have one terminal byte' % (start_location))

			elif IsKnownTrackAction(entry_type):
				read_offset += 1
				action_id = (byte_array[read_offset:read_offset+1])[0]
				read_offset += 1
				action_data = (byte_array[read_offset:read_offset+1])[0]
				read_offset += 1
				trailing_bytes = []
				trailing_bytes.append((byte_array[read_offset:read_offset+1])[0])
				self.AddSPDUsage(start_location, entry_type, action_id, action_data, trailing_bytes)
			else:
				# So far we only run into this for Powerstone2's BOSS2.BIN which is using transforms on the output
				# Lead byte is 0xBC?
				self.starting_offset = start_location
				PrintAsWarning('Warning: track entry type 0x%02x at 0x%02x is not understood.' % (entry_type, start_location))
		
		return nTotalSPDsUsed

	def AddSPDUsage(self, starting_offset, entry_type, SPD_used_low, volume, trailing_bytes):
		self.starting_offset = starting_offset
		self.entryList.append(DTPKTrackEntry(entry_type, SPD_used_low, volume, trailing_bytes))

	def SetSPDReferenced(self, spdReferenced):
		# BUGBUG: This is naive and cannot handle joined tracks
		spdReferencedLow = (spdReferenced & 0x7f)
		spdReferencedHigh = int((spdReferenced & 0xf80) / 80)
		self.raw_bytes[2:3] = array('B', spdReferenced.to_bytes(1, byteorder='little'))
		oldTerminalByte = self.raw_bytes[len(self.raw_bytes) - 1 : len(self.raw_bytes)]
		newTerminalByte = (oldTerminalByte & 0xF0) | spdReferencedHigh
		self.raw_bytes[len(self.raw_bytes) - 1 : len(self.raw_bytes)] = array('B', newTerminalByte.to_bytes(1, byteorder='little'))
		
	def GetSampleCount(self):
		sampleCount = 0
		for entry in self.entryList:
			if (IsTrackEntry(entry.entry_type)):
				if (entry.SPD_used_or_event_id != INVALID_SAMPLE_ID):
					sampleCount += 1
		return sampleCount

	def PrintParsedSongChunk(self, starting_offset, song_bytes):
		# OK, this is all highly speculative but seems to bear out based upon wild DTPKs
		# and some sample DTPKs with known contents.
		# We have a four byte opening sequence of form:
		# aa bb cc dd
		# aa: MIDI audio output method.  Values should be:
			# 0x20: External
			# 0x40: Internal
			# 0x60: Both
		# bb: Sync group
		# cc: Sync level
		# dd: Unused, 0x00
		
		# Subsequent data chunks are either:
			# 0x9x: Terminate
			# or four byte chunks.
		# The meaning of the four byte chunks are defined by byte 3
			# if 0x00: seek to byte1 | (byte 2 << 8) and play back until 0xFF 0x2F 0x00 0x00 magic sequence
			# if 0xa0: command event.  byte 2 = event type, byte 1 = event value.

		iParsePos = 0
		iOperationCount = 0
		fFirstMIDIEntry = True

		fFirstMIDIEntry = False
		if   song_bytes[iParsePos] >= 0x60:
			strOutput = "Both"
		elif song_bytes[iParsePos] >= 0x40:
			strOutput = "Internal"
		elif song_bytes[iParsePos] >= 0x20:
			strOutput = "External"
		else:
			strOutput ="[INVALID]"
		iParsePos += 1
		if song_bytes[iParsePos] == 0:
			strSyncGroup = "Off"
		else:
			strSyncGroup = "0x%02x. Sync Level: 0x%02x" % (song_bytes[iParsePos], song_bytes[iParsePos + 1])
		iParsePos += 1
		iParsePos += 1
		iParsePos += 1

		summary = '\t\t\tOutput by: %s.  Sync State: %s' % (strOutput, strSyncGroup)

		while (iParsePos < len(song_bytes)):
			if (song_bytes[iParsePos] & 0xf0) == 0x90:
				# Lead byte of what would have been a quad can be an end byte
				break
			else:
				byte1 = song_bytes[iParsePos]
				iParsePos += 1
				byte2 = song_bytes[iParsePos]
				iParsePos += 1
				byte3 = song_bytes[iParsePos]
				iParsePos += 1
				byte4 = song_bytes[iParsePos]
				iParsePos += 1

				if   byte3 == 0xa0:
					strEvent = GetStringForSystemEventType(byte3, byte2, byte1)
					summary += '\n\t\t\tOperation 0x%02x: %s' % (iOperationCount, strEvent)
					iOperationCount += 1
				elif byte3 == 0x00:
					if byte4 == 0x90:
						# Shenmue II JPN in passing.bin and magical.bin has sequence
							# 0x14 0x00 0x00 0x90
						# which we do not currently understand.  What's magic value 0x14 here?
						summary += '\n\t\t\t' + bcolors.WARNING + 'Unknown terminal sequence: 0x%02x 0x%02x 0x%02x 0x%02x' % (byte1, byte2, byte3, byte4) + bcolors.ENDC
						break
					else:
						midiOffset = (byte2 << 8) + byte1
						summary += '\n\t\t\tPlayback: Data at 0x%x from start of this entry (0x%x in file)' % (midiOffset, midiOffset + starting_offset)

					if (byte4 & 0xf0) == 0x90:
						break

		# Note that we print this at the end because we don't know data length until after parsing
		print ('\t\tSONG %02u composition from 0x%x to 0x%x:' % (self.track_id, starting_offset, starting_offset + iParsePos + 1))
		print (summary)

	def Print(self):
		if (show_detailed_data_tracks):
			if (self.bank_label == 0xA8): # SONG
				self.PrintParsedSongChunk(self.starting_offset, self.raw_bytes[0x00:len(self.raw_bytes)])
			else:
				if (len(self.entryList)):
					if (IsTrackEntry(self.entryList[0].entry_type)):
						# At least one
						print ('\t\tTrack %02u composition starting at 0x%x, using %u sequences:' % (self.track_id, self.starting_offset, len(self.entryList)))
					else:
						# Unused
						print ('\t\tTrack %02u composition starting at 0x%x (Event):' % (self.track_id, self.starting_offset))

					if   self.destination >= 0xe0:
						strOutput = "Both"
					elif self.destination >= 0xc0:
						strOutput = "Internal"
					elif self.destination >= 0xa0:
						strOutput = "External"
					else:
						strOutput ="[INVALID]"

					if self.sequencer_group & 0x10:
						strSeqGroup = '0x%02x' % (self.sequencer_group & 0x0f)
					else:
						strSeqGroup = "None"

					if self.sync_group == 0:
						strSyncGroup = "Off"
					else:
						strSyncGroup = "0x%02x. Sync Level: 0x%02x" % (self.sync_group, self.sync_level)

					if (IsTrackEntry(self.entryList[0].entry_type)):
						print ('\t\t\tOutput by: %s.  Sequencer group: %s. Sync Group: %s' % (strOutput, strSeqGroup, strSyncGroup))

					for entry in self.entryList:
						entry.Print()
				elif self.raw_bytes[0x01:0x02][0] == 0xc0:
					print ('\t\tTrack %02u Song composition starting at 0x%x:' % (self.track_id, self.starting_offset))
					print ('\t\t\tSong Track chunk from 0x%x to 0x%x.  Song Track chunks are not well understood.' % (self.starting_offset, self.starting_offset + len(self.raw_bytes)))
					print ('\t\t\tType: Song (0x%02x).  Song: Inline.' % (self.raw_bytes[0x01:0x02][0]))

				else:
					print ('\t\tTrack %02u composition starting at 0x%x:' % (self.track_id, self.starting_offset))
					PrintAsFailure('\t\t\tTrack chunk from 0x%x to 0x%x is not understood.' % (self.starting_offset, self.starting_offset + len(self.raw_bytes)))
					print('\t\t\tThis chunk has effects and transforms upon the audio samples that we can\'t parse.')

class DTPKSequencerEntry:
	def __init__(self, sequencerChunkStart, chunkDescriptor, nTotalTracksSoFar, data_chunk):

		self.chunkDescriptor = chunkDescriptor
		if (len(data_chunk)):
			countGroupItems = unpack('<I', data_chunk[0x00:0x04])[0] + 1
		else:
			# We have no data_chunk yet: that indicates that this is a freshly created group that we will populate later
			countGroupItems = 0
		self.sequencerChunkStart = sequencerChunkStart
		read_offset = self.chunkDescriptor & 0xFFFF
		bank_type = self.chunkDescriptor >> 24

		groupEntryOffsets = []
		self.groupItems = []
		for iGroupEntry in range(countGroupItems):
			groupEntryOffsets.append(unpack('<I', data_chunk[4 + (4 * iGroupEntry):8 + (4 * iGroupEntry)])[0])

		for iGroupEntry in range(countGroupItems):
			chunkShiftedStart = groupEntryOffsets[iGroupEntry]
			if ((iGroupEntry + 1) < len(groupEntryOffsets)):
				chunkEnd = groupEntryOffsets[iGroupEntry + 1] - read_offset
			else:
				chunkEnd = len(data_chunk)
				# remove padding: we will readd on output
				while data_chunk[chunkEnd - 1:chunkEnd][0] == 0x00:
					chunkEnd -= 1

			current_track = DTPKTrack(iGroupEntry, nTotalTracksSoFar) 
			nTotalTracksSoFar += 1
			nCurrentGroupOffset = self.sequencerChunkStart + chunkShiftedStart
			current_track.AddRawEntryData(bank_type, nCurrentGroupOffset, data_chunk[chunkShiftedStart - read_offset:chunkEnd])
			self.groupItems.append(current_track)

	def GetUnmaskedBankID(self):
		return (self.chunkDescriptor >> 16) & 0xff
	
	def GetUnmaskedDescriptor(self):
		return self.chunkDescriptor >> 16

	def GetUnmaskedChunkOffset(self):
		return self.chunkDescriptor & 0xFFFF

	def GetChunkTerminationOffset(self):
		pos = self.chunkDescriptor & 0xFFFF
		pos += 4 + (4 * len(self.groupItems))
		for curGroup in self.groupItems:
			pos += len(curGroup.raw_bytes)
		return pos

	def ShiftChunkOffset(self, byteDelta):
		self.chunkDescriptor += byteDelta

	def GetTrackCount(self):
		return len(self.groupItems)

	def AddTrack(self, spdUsed):
		# BUGBUG : Errata to fix?
		# Note that normally for DTPKTrack creation parameter 1 should be relative number
		# and parameter 2 should be absolute across all groups.
		newTrack = DTPKTrack(len(self.groupItems), len(self.groupItems))

		fUsingActualTrack = spdUsed != 0xbad

		fFoundUsefulExample = False
		for curItem in self.groupItems:
			if (curItem.GetSampleCount() == 1):
				fFoundUsefulExample = True
				if fUsingActualTrack:
					newTrack.AddRawEntryData(0xA9, 0, array('B', bytes(curItem.raw_bytes)))
				else:
					# start with empty template
					rawdata = [0xC0, 0xA0, 0x00, 0x80, 0xFF]
					# copy the preferred start and terminal bytes from an existing entry
					# this shouldn't really matter, but -- best to hew closest to existing
					# style and values
					rawdata[0] = curItem.raw_bytes[0x00]
					rawdata[len(rawdata) - 1] = curItem.raw_bytes[len(curItem.raw_bytes) - 1]
					newTrack.AddRawEntryData(0xA9, 0, array('B', bytes(rawdata)))

		if (not fFoundUsefulExample):
			# Specifically the initial and penultimate byte sometimes vary.
			# We tried templating using a member of this group but didn't find anything.
			# If we really cared we could scan other groups for viable examples
			PrintAsWarning('Warning: added track 0x%02x was constructed by hand and may not be in alignment with other tracks.' % len(self.groupItems))
			if fUsingActualTrack:
				rawData = [0xC0, 0xDF, 0x00, 0x7F, 0x80, 0xFF]
				rawData[2:3] = spdUsed.to_bytes(1, byteorder='little')
			else:
				rawdata = [0xC0, 0xA0, 0x00, 0x80, 0xFF]
			newTrack.AddRawEntryData(0xA9, 0, array('B', bytes(rawData)))

		if fUsingActualTrack:
			newTrack.SetSPDReferenced(spdUsed)
		self.groupItems.append(newTrack)
		return (len(newTrack.raw_bytes))

	def PrintTableOffsets(self, this_chunk_offset):
		if show_detailed_data_tracks:
			totalHandledEntryLength = 0
			for iPos, iCurEntry in enumerate(self.groupItems):
				offsetTableLoc = this_chunk_offset + (4 * iPos)
				entryLoc = this_chunk_offset + (4 * len(self.groupItems)) + totalHandledEntryLength
				entryRelativeLoc = entryLoc - self.sequencerChunkStart
				totalHandledEntryLength += len(iCurEntry.raw_bytes)
				print ('\t\tTrack data %2u (0x%x): Definition starts at 0x%x (0x%x from sequencer table start)' % (iPos, offsetTableLoc, entryLoc, entryRelativeLoc))

	def PrintTableEntries(self, nCurrentGroupOffset, nTotalTracksSoFar):
		for curGroup in self.groupItems:
			curGroup.Print()

# For a DTPKSequencerChunk, consider the 44 track sample (PL0A_VOI 'rogue' from mvc2-dc):

# We have N sets of tables based upon how many sequencer groups there are.
# First at 0x1a8 is a 0-based 4 byte value indicating sequencer group count.  
# After that we have a 4 byte value per sequencer group.
# 0x0000ffff is the offset for that group within the sequencer table
# 0xffff0000 is the group_type OR'd with another value

# Each Group starts with a 4 byte value indicating the number of Tracks in that group.
# Each used Track is associated with a mostly unique sample playback definition in that table.
# VTennis does reuse SPDs in two instances (PACK1.bin and PACK8.bin) but in both cases
# the second usage is just a muted (vol 0x00) version of the SPD.
# Note that each SPD block is padded to a terminal 4 byte boundary

# So looking at PL0A / Rogue:
	# Sequencer 1 data starts after track count at 0x1b4:
		# Track Composition Read Offset data group 1 from 0x1b4-0x268
			# 4 bytes per track.
			# Each entry is the start_byte starting from start_sequencer_chunk for that specific entry
			# Consider:
			# c0 00 00 00 c6 00 00 00 cc 00 00 00
			# Track 0 starts at offset 0xc0 within the TCD.  Track 1 starts at offset 0x0c6.  Track 2 starts at offset 0xcc.
		# Track Composition Data from 0x268-0x358 // len 0xf0 / 240 .  Variable length per track, each section ends with FF
	# We are now in data for sequencer group 2 as of 0x358:
		# Track Composition Read Offset data group 1 0x358-0x40c // len b4 / 180
		# Track Composition Data from 0x40c-0x4fc // 0xf0 // 240

class DTPKSequencerChunk:
	def __init__(self, sequencerChunkStart, data_chunk):
		self.sequencerChunkStart = sequencerChunkStart
		self.SequencerGroups = []

		if len(data_chunk):
			countSequencerGroups = unpack('<I', data_chunk[0x00:0x04])[0] + 1

			groupChunkOffsets = []
			for iGroup in range(countSequencerGroups):
				groupChunkOffsets.append(unpack('<I', data_chunk[4 + (4 * iGroup):8 + (4 * iGroup)])[0])

			nTotalTracksSoFar = 0
			for iGroup in range(countSequencerGroups):
				chunkMaskedStart = groupChunkOffsets[iGroup]
				if ((iGroup + 1) < len(groupChunkOffsets)):
					chunkMaskedEnd = groupChunkOffsets[iGroup + 1]
				else:
					chunkMaskedEnd = len(data_chunk)

				self.SequencerGroups.append(DTPKSequencerEntry(sequencerChunkStart, chunkMaskedStart, nTotalTracksSoFar, data_chunk[(chunkMaskedStart & 0xFFFF):(chunkMaskedEnd & 0xFFFF)]))
				nTotalTracksSoFar += self.SequencerGroups[iGroup].GetTrackCount()

	def GetGroupCount(self):
		return len(self.SequencerGroups)

	def GetTotalTrackCount(self):
		nTotalTracksSoFar = 0
		for curGroup in self.SequencerGroups:
			nTotalTracksSoFar += curGroup.GetTrackCount()
		return nTotalTracksSoFar

	def FindSequencerEntry(self, playback_id):
		totalPlaybacksReferenced = 0

		group_id = track_id = 0xFFFF

		for iGroupPos, curGroup in enumerate(self.SequencerGroups):
			for iPos, curTrack in enumerate(curGroup.groupItems):
				totalPlaybacksReferenced += curTrack.GetSampleCount()
				if (playback_id < totalPlaybacksReferenced):
					track_id = iPos
					group_id = iGroupPos
					break
			if (track_id != 0xFFFF):
				break

		return group_id, track_id

	def HandleInjectedTracks(self, addedTracks):
		if (len(addedTracks)):
			print ('Injecting 0x%02x track(s):' % len(addedTracks))
			# Keep count of tracks per group to account for padding
			totalNewTracksThisGroup = [0x00] * len(self.SequencerGroups)
			for newTrack in addedTracks:
				if (newTrack.referencedSPD > 0x100) and (newTrack.referencedSPD != 0xbad):
					PrintAsFailure('ERROR: Max value of an SPD ID is 255 / 0xFF: 0x%x requested.  Failing this add.' % newTrack.referencedSPD)
					break
				elif (newTrack.groupDestination < 0xa900):
					PrintAsFailure('ERROR: Unsupported group ID: 0x%x requested.  We only support adding to 0xA9-0xAD.  Failing this add.' % newTrack.groupDestination)
					PrintAsFailure('Use the four byte description ID seen in the Sequencer Data output.')
					if newTrack.groupDestination < len(self.SequencerGroups):
						PrintAsFailure('You probably want group ID \'%x\'.' % (self.SequencerGroups[newTrack.groupDestination].GetUnmaskedDescriptor()))
					break
				elif ((newTrack.groupDestination > 0xa900) and (((newTrack.groupDestination & 0xFF) > 0x7f) or (newTrack.groupDestination >= 0xae00))):
					PrintAsFailure('ERROR: Max value of a group descriptor ID is is 0xad7f: 0x%x requested.  Failing this add.' % newTrack.groupDestination)
					break
				elif (newTrack.groupDestination < len(self.SequencerGroups)):
					# add the new track via absolute index
					byteShift = 4 + self.SequencerGroups[newTrack.groupDestination].AddTrack(newTrack.referencedSPD)
					totalNewTracksThisGroup[newTrack.groupDestination] += 1
					for iGroupToShift in range(len(self.SequencerGroups)):
						if (iGroupToShift > newTrack.groupDestination):
							# move the location of all subsequent chunks by the new shift
							self.SequencerGroups[iGroupToShift].ShiftChunkOffset(byteShift)
					print ('\tAdded new track %u (0x%02x) to group 0x%02x.  It uses SPD %u (0x%02x).' % (self.SequencerGroups[newTrack.groupDestination].GetTrackCount() - 1, self.SequencerGroups[newTrack.groupDestination].GetTrackCount() - 1, newTrack.groupDestination, newTrack.referencedSPD, newTrack.referencedSPD))
				elif ((newTrack.groupDestination >= len(self.SequencerGroups)) and (newTrack.groupDestination < 0xa900)):
					PrintAsFailure('ERROR: If adding a new group by absolute index, you must add them via a new group descriptor ID.  Failing this add.')
				elif (newTrack.groupDestination >= 0xa900):
					# add the track to either an existing or new group via group descriptor
					iTargetGroup = INVALID_SAMPLE_ID
					for iGroupToUse in range(len(self.SequencerGroups)):
						if (self.SequencerGroups[iGroupToUse].GetUnmaskedDescriptor() == newTrack.groupDestination):
							iTargetGroup = iGroupToUse
					if (iTargetGroup != INVALID_SAMPLE_ID):
						# existing
						byteShift = 4 + self.SequencerGroups[iTargetGroup].AddTrack(newTrack.referencedSPD)
						totalNewTracksThisGroup[iTargetGroup] += 1
						for iGroupToShift in range(len(self.SequencerGroups)):
							if (iGroupToShift > iTargetGroup):
								# move the location of all subsequent chunks by the new shift
								self.SequencerGroups[iGroupToShift].ShiftChunkOffset(byteShift)
						if newTrack.referencedSPD != 0xbad:
							print ('\tAdded new track %u (0x%02x) to group 0x%02x (0x%04x).  It uses SPD %u (0x%02x).' % (self.SequencerGroups[iTargetGroup].GetTrackCount() - 1, self.SequencerGroups[iTargetGroup].GetTrackCount() - 1, iTargetGroup, newTrack.groupDestination, newTrack.referencedSPD, newTrack.referencedSPD))
						else:
							print ('\tAdded new track %u (0x%02x) to group 0x%02x (0x%04x).  This is a NULL/placeholder track.' % (self.SequencerGroups[iTargetGroup].GetTrackCount() - 1, self.SequencerGroups[iTargetGroup].GetTrackCount() - 1, iTargetGroup, newTrack.groupDestination))
					else:
						# new group
						lastGroupLocation = 0
						for iGroupToShift in range(len(self.SequencerGroups)):
							# move everybody four bytes for this new group
							self.SequencerGroups[iGroupToShift].ShiftChunkOffset(4)
							lastGroupLocation = self.SequencerGroups[iGroupToShift].GetChunkTerminationOffset()

						remnant = lastGroupLocation % 4
						lastGroupLocation += remnant
						chunkMaskedStart = (newTrack.groupDestination << 16) | (lastGroupLocation)

						print('\tAdded new group 0x%x at index 0x%02x at location 0x%x' % (newTrack.groupDestination, len(self.SequencerGroups), self.sequencerChunkStart + lastGroupLocation))
						self.SequencerGroups.append(DTPKSequencerEntry(self.sequencerChunkStart, chunkMaskedStart, self.GetTotalTrackCount(), []))
						self.SequencerGroups[len(self.SequencerGroups) - 1].AddTrack(newTrack.referencedSPD)
						# Indicate that we have no tracks (yet!) in this group
						totalNewTracksThisGroup.append(0x01)
				else:
					PrintAsFailure('ERROR: Add an error message here.')

			for curGroup in range(len(self.SequencerGroups)):
				# handle padding/remnants from adjusted chunks
				# Be careful about chunks that *had* padding that no longer need it
				# vtennis-dc pack0.bin group 5 is useful for this, has 3 bytes padding
				fragment = totalNewTracksThisGroup[curGroup] % 2
				if (fragment != 0):
					for remainingGroups in range(curGroup + 1, len(self.SequencerGroups)):
						self.SequencerGroups[remainingGroups].ShiftChunkOffset(2)

	def GetBytes(self):
		outBytes = []
		nCountGroups = len(self.SequencerGroups)
		if (nCountGroups > 0):
			paddingForGroup = [0x00] * nCountGroups
			nCountGroups -= 1
			outBytes = array('B', nCountGroups.to_bytes(4, byteorder='little'))
			nOffsetToEntry = 4
			nPreviousGroupTerminalOffset = 0
			for iGroupPos, curGroup in enumerate(self.SequencerGroups):
				if nPreviousGroupTerminalOffset > 0:
					# Handle group padding changes here
					if (nPreviousGroupTerminalOffset != curGroup.GetUnmaskedChunkOffset()):
						delta = nPreviousGroupTerminalOffset - curGroup.GetUnmaskedChunkOffset()
						curGroup.ShiftChunkOffset(delta)

				outBytes += array('B', curGroup.chunkDescriptor.to_bytes(4, byteorder='little'))
				nPreviousGroupTerminalOffset = curGroup.GetChunkTerminationOffset()
				# We track the padding-based chunk deltas here so we can adjust the offset pointers,
				# and then inject the padding in the next chunk
				paddingForGroup[iGroupPos] = (4 - (nPreviousGroupTerminalOffset % 4)) % 4
				if paddingForGroup[iGroupPos] != 0:
					nPreviousGroupTerminalOffset += paddingForGroup[iGroupPos]
				
				nOffsetToEntry += 4

			for iGroupPos, curGroup in enumerate(self.SequencerGroups):
				if (len(curGroup.groupItems)):
					nCountGroupItems = len(curGroup.groupItems) - 1
					outBytes += array('B', nCountGroupItems.to_bytes(4, byteorder='little'))
					# Walk past the group's track count
					nOffsetToEntry += 4
					# Walk past the group's track offset chunk
					nOffsetToEntry += (len(curGroup.groupItems) * 4)
					for iPos, curTrack in enumerate(curGroup.groupItems):
						outBytes += array('B', nOffsetToEntry.to_bytes(4, byteorder='little'))
						nOffsetToEntry += len(curTrack.raw_bytes)
					for iPos, curTrack in enumerate(curGroup.groupItems):
						outBytes += curTrack.raw_bytes

					if paddingForGroup[iGroupPos] != 0:
						nOffsetToEntry += paddingForGroup[iGroupPos]
						outBytes += array('B', bytes(paddingForGroup[iGroupPos]))
		else:
			print('(No sequencer groups present.)')

		return outBytes

	def PrintLostTracks(self):
		# This helper functionality shows tracks that might have been lost if the DTPK
		# was extracted with the old version of dsfdtpk
		firstGroupCount = self.SequencerGroups[0].GetTrackCount()
		for iGroup, curGroup in enumerate(self.SequencerGroups):
			if firstGroupCount < curGroup.GetTrackCount():
				PrintAsFailure('ERROR: %u tracks potentially lost for group 0x%02x' % (curGroup.GetTrackCount() - firstGroupCount, iGroup))

	def Print(self):
		print ('\n  ==================SEQUENCER DATA===================')
		if self.GetGroupCount() == 0:
			print ('(No sequencer data present.)')
		else:
			print ('Group count:\t\t%u\t\t\t\t\t\t0x%x' % (self.GetGroupCount(), self.sequencerChunkStart))

			for iGroup, curGroup in enumerate(self.SequencerGroups):
				currentSequencerCommandLoc = self.sequencerChunkStart + 4 + (4 * iGroup)

				cmd_type = curGroup.GetUnmaskedDescriptor()
				cmd_offset = curGroup.GetUnmaskedChunkOffset()

				print ('Group %u description:\t0x%x\t\t\t\t\t\t0x%x' % (iGroup, cmd_type, currentSequencerCommandLoc))
				areaDescriptor = cmd_type >> 8
				if (areaDescriptor == 0xA8):
					print ('\tType is:\t0xA8: SONG')
				elif (areaDescriptor == 0xA9):
					print ('\tType is:\t0xA9: SFX (area 1)')
				elif (areaDescriptor == 0xAA):
					print ('\tType is:\t0xAA: SFX (area 2)')
				elif (areaDescriptor == 0xAB):
					print ('\tType is:\t0xAB: SFX (area 3)')
				elif (areaDescriptor == 0xAC):
					print ('\tType is:\t0xAC: SFX or SONG (area 1)')
				elif (areaDescriptor == 0xAA):
					print ('\tType is:\t0xAD: SFX or SONG (area 2)')
				else:
					PrintAsFailure('ERROR: Type is:\tUnknown group type 0x%x' % areaDescriptor)
				print ('\tBank ID:\t0x%02x (%u)' % ((cmd_type & 0xff), (cmd_type & 0xff)))
				print ('\tData starts at: 0x%03x (0x%03x + 0x%03x)' % (cmd_offset + self.sequencerChunkStart, self.sequencerChunkStart, cmd_offset))

			print ('\nSequencer group data (0x%03x):' % (self.SequencerGroups[0].GetUnmaskedChunkOffset() + self.sequencerChunkStart))

			nTotalTracksSoFar = 0
			for iGroup, curGroup in enumerate(self.SequencerGroups):
				startEntryOffsetsTable = self.sequencerChunkStart + curGroup.GetUnmaskedChunkOffset()
				startEntriesTable = startEntryOffsetsTable + 4
				print ('\tGroup %u count tracks:\t\t%u (0x%02x)\t\t\t0x%03x' % (iGroup, curGroup.GetTrackCount(), curGroup.GetTrackCount(), startEntryOffsetsTable))
				print ('\tGroup %u track composition offset data:\t\t\t\t0x%03x' % (iGroup, startEntriesTable))

				curGroup.PrintTableOffsets(startEntriesTable)

				nCurrentGroupOffset = startEntryOffsetsTable +  4 + (4 * curGroup.GetTrackCount())
				print ('\tGroup %u track composition data:\t\t\t\t\t0x%03x' % (iGroup, nCurrentGroupOffset))
				curGroup.PrintTableEntries(nCurrentGroupOffset, nTotalTracksSoFar)
				nTotalTracksSoFar += curGroup.GetTrackCount()

class DTPKPlaybackDataEntry:
	# Speculative format of the 64 byte track composition block
	# This is fairly well understood (see the Print function), but leaving this miniguide
	# here for reference
	# Byte 00: track ID.  Note that values are 0x00-0xFF ... and then apparently the high bits just overflow and are not contained in the file.
			# So track those manually.
	# Byte 01: Unknown, usually 80
	# Byte 02: sample bank ID used 
	# Byte 03: Unknown, only values 1 or 2 seem to be used
	# Byte 04: Adjust, low byte, settable 0 to beyond 0x265, didn't test limit
	# Byte 05: Adjust, high byte
	# Byte 06: Panning. Def 0x80 (combi).  Value 0x1f == left channel only.  Value 0x0f == right channel only.
			# 0x00 == center
	# Byte 07: Range/Gain: 0x00-0F, default 0x0c
	# Byte 08: 
	# Byte 09: 
	# Byte 10: Rate/frequency high 8 | base note | transposition
	# Byte 11: Rate/frequency low 8 | detune.  note that detune overflows into byte 10
	# Byte 12: default 00.  attack sustain decay bits
	# Byte 13: default 1f.  attack sustain decay bits
	# Byte 14: default 00.  attack sustain decay bits
	# Byte 15: default 00.  attack sustain decay bits
	# Byte 16: default 00.  low pass filter bits: frequency 0

	# Byte 17: fd low pass filter bits: frequency 0 & attack rate
	# Byte 18: 07 low pass filter bits: frequency 1
	# Byte 19: fd low pass filter bits: frequency 1 & decay rate 1
	# Byte 20: 07 low pass filter bits: frequency 2
	# Byte 21: fd low pass filter bits: frequency 2 & decay rate 2
	# Byte 22: 07 low pass filter bits: frequency 3
	# Byte 23: fd low pass filter bits: frequency 3 & release rate
	# Byte 24: 27 low pass filter bits: frequency 4
	# Byte 25: 00 low pass filter bits: frequency 4
	# Byte 26: 00
	# Byte 27: 00
	# Byte 28: 00 Random Tune.  def. 00.  values 0-0f
	# Byte 29: 00  
	# Byte 30: 00 fx override
	# Byte 31: 7f fx flags (channel & send)
	# Byte 32: 00

	# Byte 33: 00
	# Byte 34: 00 group override
	# Byte 35: 00 group priority. def 00. 0-127.  0x7f== c127
	# Byte 36: 00 self priority. def 00.   0-127, P0
	# Byte 37: 00	
	# Byte 38: 00
	# Byte 39: 00
	# Byte 40: 00
	# Byte 41: 00
	# Byte 42: 00
	# Byte 43: 00
	# Byte 44: 00
	# Byte 45: 00
	# Byte 46: 00 low frequency oscillation: Delay. Def 00.  0-255
	# Byte 47: 80 low frequency oscillation: Feed. Def 00.  0-255
	# Byte 48: 00 low frequency oscillation: form & depth flags

	# Byte 49: 00 low frequency oscillation: sync & speed
	# Byte 50: 00
	# Byte 51: 00
	# Byte 52: 00
	# Byte 53: 00
	# Byte 54: 00
	# Byte 55: 00
	# Byte 56: 00
	# Byte 57: 00
	# Byte 58: 00
	# Byte 59: 00
	# Byte 60: 00
	# Byte 61: 00
	# Byte 62: 00
	# Byte 63: 00
	def __init__(self, file_offset, data_chunk, total_playbacks):
		self.file_offset = file_offset
		self.data_chunk = data_chunk
		self.playbackIDHighBits = (total_playbacks & 0xff00) >> 8

	def GetPlaybackIDFull(self):
		playbackID = self.data_chunk[0:1][0]
		playbackID += self.playbackIDHighBits << 8
		
		return playbackID

	def GetFileAbsolutePlaybackLocation(self):
		# BUGBUG: this should be volatile
		return self.file_offset

	def GetSampleUsed(self):
		# Note that there can be a conflict(?) between the Track Composition Data sample value and 
		# the Track Playback Data sample value.  For at least reDream, the non-zero value of the two is ignored.
		# In the instance where both are non-zero, the Track Composition Data value is used.
		return (self.data_chunk[2:3])[0]

	def GetRate(self):
		# Note that Rate is actually a 16bit value.  See the note below in Print
		return (self.data_chunk[10:11])[0]

	def GetRate16(self):
		# Note that Rate is actually a 16bit value.  See the note below in Print
		return ((self.data_chunk[10:11])[0] << 8) + (self.data_chunk[11:12])[0]

	def SetRate(self, new_rate):
		self.data_chunk[10:12] = array('B', new_rate.to_bytes(2, byteorder='big'))

	def Print(self):
		print ('\t\tPlayback 0x%02x:\t\t\t\t\t\t0x%03x' % (self.GetPlaybackIDFull(), self.GetFileAbsolutePlaybackLocation()))
		print ('\t\t\tSample:\t\t\t%u (0x%02x)' % (self.GetSampleUsed(), self.GetSampleUsed()))

		#######################################################################################################
		# PCM adjustments
		#######################################################################################################
		print ('\t\t\tRate:\t\t\t0x%04x (%5uHz)' % (self.GetRate16(), TranslateDTPKRate(self.GetRate16())))
		nAdjust = self.data_chunk[4:6][0]
		PrintValueIfWarranted('\t\t\t%s:\t\t\t%u' % ('Adjust', nAdjust), 0, nAdjust)

		# RATE value is interesting.  Raw rate is mostly understood.
		# However! RATE is a 16bit number that also incorporates BASE note and 
		# TRANSPOSITION values in the high 8. The low 8 incorporates the Detune value.
		# We do not have a way to unpack that: those are handled as adjustments to the 
		# rate as far as we currently know.

		# This following logic is correct in a vacuum, but as noted the byte incorporates bits from Rate
		# and as such we should not display/note Detune as we are likely to be wrong.
		# detune is also interesting.  if it overflows it adds +2 to offset 0n10
		#nDetune = self.data_chunk[11:12][0]
		#if nDetune > 0x7f:
			#nDetune = ((0xFF + 0x1F) - nDetune) * -1
		#else:
			#nDetune = nDetune - 0x1e
		#PrintValueIfWarranted('\t\t\t%s:\t\t\t%i' % ('Detune', nDetune), 0, nDetune)

		nRandomTune = self.data_chunk[28:29][0]

		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('RandomTune', nRandomTune), 0, nRandomTune)

		#######################################################################################################
		# Output adjustments
		#######################################################################################################

		nPanValue = self.data_chunk[6:7][0]
		if nPanValue == 0x00:
			strPan = "Center"
		elif nPanValue == 0x80:
			strPan = "Combi"
		elif nPanValue >= 0x10:
			strPan = "Left: %u" % (nPanValue & 0x0f)
		else:
			strPan = "Right: %u" % (nPanValue & 0x0f)

		PrintValueIfWarranted('\t\t\t%s:\t\t\t%s' % ('Pan', strPan), 0x80, nPanValue)

		nGainValue = self.data_chunk[7:8][0]
		if nGainValue == 0x80:
			strGain = 'Combi'
		else:
			strGain = "%u" % (nGainValue)

		PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('Range aka Gain', strGain), 12, nGainValue)

		#######################################################################################################
		# Envelope generation
		#######################################################################################################

		nDecayLevel = 0
		nDecay2 = 0
		nReleaseRate = 0
		nKeyRate = 0

		attackSustainDecayBits1 = self.data_chunk[12:13][0]
		attackSustainDecayBits2 = self.data_chunk[13:14][0]
		attackSustainDecayBits3 = self.data_chunk[14:15][0]
		attackSustainDecayBits4 = self.data_chunk[15:16][0]

		nAttack = attackSustainDecayBits1 & 0x1f
		nDecay1 = (attackSustainDecayBits1 >> 6) + ((attackSustainDecayBits2 & 0x07) << 2)
		
		nDecayLevelLowBits = (((attackSustainDecayBits3 | 0x1f) ^ 0xff) ) >> 5
		nDecayLevelHighBits = ((attackSustainDecayBits4 ^ 0xff) & 0x03) << 3
		nDecayLevel = nDecayLevelLowBits + nDecayLevelHighBits
		
		nDecay2 = ((attackSustainDecayBits2 >> 3))

		nReleaseRate = attackSustainDecayBits3 & 0x1f
		nKeyRate = (attackSustainDecayBits4 & 0x3c) >> 2
		nLPLink = (attackSustainDecayBits4 >> 6) & 0x01

		if nLPLink:
			strLPLink = "On"
		else:
			strLPLink = "Off"

		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Attack Rate', nAttack), 31, nAttack )
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Decay Rate 1', nDecay1), 0, nDecay1)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Decay Level', nDecayLevel), 31, nDecayLevel)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Decay Rate 2', nDecay2), 0, nDecay2)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Release Rate', nReleaseRate), 31, nReleaseRate)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('Key rate scaling', nKeyRate), 0, nKeyRate)
		PrintValueIfWarranted('\t\t\t%s:\t\t\t%s' % ('LPLink', strLPLink), 0, nLPLink)

		#######################################################################################################
		# Filter Enveloper Generator (FEG) / Low Pass Filter (LPF) stuff
		#######################################################################################################

		# interpreted values -3 - 20.25db
		# raw value default 4, 0-31
		nLPFQ = (self.data_chunk[25:26][0] & 0xf8) >> 3

		nLPFFlags1 = self.data_chunk[16:17][0]
		nLPFFlags2 = self.data_chunk[17:18][0]

		# default 0, 0-127
		nLPFFrequency0 = TranslateDTPKPacked127Value(((nLPFFlags2 & 0x07) << 8) + nLPFFlags1)
		# default 0, 0-31
		nLPFAttackRate = (nLPFFlags2 & 0xf8) >> 3

		nLPFFlags1 = self.data_chunk[18:19][0]
		nLPFFlags2 = self.data_chunk[19:20][0]

		# default 127, 0-127
		nLPFFrequency1 = TranslateDTPKPacked127Value(((nLPFFlags2 & 0x07) << 8) + nLPFFlags1)
		# default 0, 0-31
		nLPFDecayRate1 = (nLPFFlags2 & 0xf8) >> 3

		nLPFFlags1 = self.data_chunk[20:21][0]
		nLPFFlags2 = self.data_chunk[21:22][0]

		#default 127, 0-127
		nLPFFrequency2 = TranslateDTPKPacked127Value(((nLPFFlags2 & 0x07) << 8) + nLPFFlags1)
		# default 0, 0-31
		nLPFDecayRate2 = (nLPFFlags2 & 0xf8) >> 3

		nLPFFlags1 = self.data_chunk[22:23][0]
		nLPFFlags2 = self.data_chunk[23:24][0]

		# default 127, 0-127
		nLPFFrequency3 = TranslateDTPKPacked127Value(((nLPFFlags2 & 0x07) << 8) + nLPFFlags1)
		# default 0, 0-31
		nLPFReleaseRate = (nLPFFlags2 & 0xf8) >> 3

		nLPFFlags1 = self.data_chunk[24:25][0]
		nLPFFlags2 = self.data_chunk[25:26][0]

		# default 127, 0-127
		nLPFFrequency4 = TranslateDTPKPacked127Value(((nLPFFlags2 & 0x07) << 8) + nLPFFlags1)

		PrintValueIfWarranted('\t\t\t%s:\t\t\t%u' % ('LPF Q', nLPFQ), 4, nLPFQ)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Frequency 0', nLPFFrequency0), 0, nLPFFrequency0)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Attack rate', nLPFAttackRate), 0, nLPFAttackRate)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Frequency 1', nLPFFrequency1), 127, nLPFFrequency1)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Decay rate 1', nLPFDecayRate1), 0, nLPFDecayRate1)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Frequency 2', nLPFFrequency2), 127, nLPFFrequency2)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Decay rate 2', nLPFDecayRate2), 0, nLPFDecayRate2)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Frequency 3', nLPFFrequency3), 127, nLPFFrequency3)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Release rate', nLPFReleaseRate), 0, nLPFReleaseRate)
		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LPF Frequency 4', nLPFFrequency4), 127, nLPFFrequency4)

		#######################################################################################################
		# Low frequency oscillation settings
		#######################################################################################################

		# default 0, 0-255
		nLFODelay = self.data_chunk[46:47][0] 
		# default 0, 0-255
		nLFOFeed = self.data_chunk[47:48][0] 
		nLFODepthFlags = self.data_chunk[48:49][0]
		nLFOFormSyncFlags = self.data_chunk[49:50][0]
		# default Off, off, 1-7
		nPitchDepth = (nLFODepthFlags & 0xf0) >> 5
		# default form Saw, saw/square/triangle/noise
		nPitchWaveForm = nLFOFormSyncFlags & 0x03
		if nPitchWaveForm == 0:
			strPitchWaveForm = '0 (Saw)'
		elif nPitchWaveForm == 1:
			strPitchWaveForm = '1 (Square)'
		elif nPitchWaveForm == 2:
			strPitchWaveForm = '2 (Triangle)'
		elif nPitchWaveForm == 3:
			strPitchWaveForm = '3 (Noise)'
		else:
			strPitchWaveForm = '[INVALID]'

		# default Off, off, 1-7
		nAmplifierDepth = nLFODepthFlags & 0x7
		# default form Saw, saw/square/triangle/noise
		nAmplifierWaveForm = (nLFODepthFlags >> 3) & 0x03
		if nAmplifierWaveForm == 0:
			strAmplifierWaveForm = '0 (Saw)'
		elif nAmplifierWaveForm == 1:
			strAmplifierWaveForm = '1 (Square)'
		elif nAmplifierWaveForm == 2:
			strAmplifierWaveForm = '2 (Triangle)'
		elif nAmplifierWaveForm == 3:
			strAmplifierWaveForm = '3 (Noise)'
		else:
			strAmplifierWaveForm = '[INVALID]'

		# default 0, 0-31
		nLFOSpeed = (nLFOFormSyncFlags & 0x7f) >> 2
		# default On, on/off
		nLFOSync = (nLFOFormSyncFlags & 0x80) >> 0x7

		PrintValueIfWarranted('\t\t\t%s:\t%u' % ('LFO Pitch Depth', nPitchDepth), 0, nPitchDepth)
		PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('LFO Pitch Form', strPitchWaveForm), 0, nPitchWaveForm)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('LFO Amp Depth', nAmplifierDepth), 0, nAmplifierDepth)
		PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('LFO Amp Form', strAmplifierWaveForm), 0, nAmplifierWaveForm)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('LFO Speed', nLFOSpeed), 0, nLFOSpeed)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('LFO Sync', nLFOSync), 1, nLFOSync)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('LFO Delay', nLFODelay), 0, nLFODelay)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('LFO Feed', nLFOFeed), 0, nLFOFeed)

		#######################################################################################################
		# More output settings
		#######################################################################################################

		nFXOverride = self.data_chunk[30:31][0]
		nFXFlags = self.data_chunk[31:32][0]
		nFXChannel = nFXFlags & 0x0f
		nFXSend = (nFXFlags & 0xf0) >> 4

		if nFXOverride == 0x80:
			strFXChannel = "Combi"
			PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('FX Channel', strFXChannel), 0, nFXOverride)
			PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('FX Send', nFXSend), 0, nFXSend)
		else:
			PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('FX Channel', nFXChannel), 0, nFXChannel)
			PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('FX Send', nFXSend), 0, nFXSend)

		nGroupOverrideFlags = self.data_chunk[34:35][0]
		nGroupPriority = self.data_chunk[35:36][0]
		nSelfPriority = self.data_chunk[36:37][0]

		strSelfPriority = '%u' % (nSelfPriority)

		if nGroupOverrideFlags == 0x00:
			strGroupSettings = "No group / polyphonic"
		elif nGroupOverrideFlags == 0x40:
			strGroupSettings = "No group / polyphonic"
			strSelfPriority = 'P0'
		elif nGroupOverrideFlags == 0x80:
			strGroupSettings = "Mono / last priority for self "
		elif nGroupOverrideFlags == 0x81:
			strGroupSettings = "Last arrival priority"
			# random storage aspect, I don't know why either
			nGroupPriority -= 1
		elif nGroupOverrideFlags == 0x82:
			strGroupSettings = "First arrival priority"
		elif nGroupOverrideFlags == 0xc0:
			strGroupSettings = "Mono / last priority for self"
			strSelfPriority = 'P0'
		elif nGroupOverrideFlags == 0xc1:
			strGroupSettings = "Last arrival priority"
			strSelfPriority = 'P0'
			nGroupPriority -= 1
		elif nGroupOverrideFlags == 0xc2:
			strGroupSettings = "First arrival priority"
			strSelfPriority = 'P0'
		else:
			strGroupSettings = "[INVALID]"

		PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('Group Settings', strGroupSettings), 0, nGroupOverrideFlags)
		PrintValueIfWarranted('\t\t\t%s:\t\t%u' % ('Group Priority', nGroupPriority), 0, nGroupOverrideFlags)
		PrintValueIfWarranted('\t\t\t%s:\t\t%s' % ('SE Priority', strSelfPriority), strSelfPriority, '0')



class DTPKPlaybackDataChunk:
	# The playback data chunk starts with a 0n80 / 0x50 sample playback definition header.
	# The only important here is at byte 0x10, which is the number of sample playback definitions.
	# We then have 0x40 bytes per playback definition

	# So in PL0A / Rogue we have Track Playback Data from 0x54c-0xf4c in 64byte chunks for those 44 tracks
	# The section is length 0xa00  / 0n2560
	def __init__(self, file_offset, data_chunk):
		self.file_offset = 0
		self.headerChunk = []
		self.PlaybackEntries = []

		totalPlaybacks = 0

		if (file_offset):
			self.file_offset = file_offset
			self.headerChunk = data_chunk[0x00:0x50]

			countPlaybackData = unpack('<H', data_chunk[0x10:0x12])[0] + 1

			data_offset = 0x50
			for iPlayback in range(countPlaybackData):
				self.PlaybackEntries.append(DTPKPlaybackDataEntry(self.file_offset + data_offset, data_chunk[data_offset:data_offset + 0x40], totalPlaybacks))
				data_offset += 0x40
				totalPlaybacks += 1

	def GetBytes(self):
		outBytes = self.headerChunk
		for curPlayback in self.PlaybackEntries:
			outBytes += curPlayback.data_chunk
		return outBytes

	def UpdatePlaybackCount(self, countTotalSPDs):
		self.headerChunk[0x10:0x12] = array('B', countTotalSPDs.to_bytes(2, byteorder='little'))

	def HandleInjectedSPDs(self, updatedPlaybacksList):
		if (len(updatedPlaybacksList)):
			print('Injecting 0x%02x new playback(s):' % len(updatedPlaybacksList))

			for curPlayback in updatedPlaybacksList:
				sampleOffset = self.PlaybackEntries[len(self.PlaybackEntries) - 1].file_offset + 0x40
				# BUGBUG: note that we're cribbing from the previous entry.  This might be contextually 
				# correct, but we could also inherit wonky Pan values and that sort of thing.  How should
				# we let the user decide whether to inherit or use stock values?
				fUseStockSPDTemplate = False

				stockSPDEntry = [ 0x00, 0x80, 0x00, 0x01, 0x00, 0x00, 0x80, 0x0c,
								  0xff, 0xff, 0x00, 0x00, 0x1f, 0x00, 0x1f, 0x00,

								  0x00, 0x00, 0xfd, 0x07, 0xfd, 0x07, 0xfd, 0x07,
								  0xfd, 0x27, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,

								  0x00, 0x7f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
								  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,

								  0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
								  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
					 ]

				if fUseStockSPDTemplate:
					sampleEntry = array('B', bytes(stockSPDEntry))
				else:
					sampleEntry = array('B', bytes(self.PlaybackEntries[len(self.PlaybackEntries) - 1].data_chunk))

				# Now adjust this template for our specific scenario
				sampleEntry[0x00:0x01] = array('B', len(self.PlaybackEntries).to_bytes(1, byteorder='little'))
				sampleEntry[0x02:0x03] = array('B', curPlayback.targetSample.to_bytes(1, byteorder='little'))

				translatedRate = curPlayback.targetRate
				if (translatedRate > 0x100):
					translatedRate = TranslateDTPKRate(translatedRate)

				sampleEntry[0x0a:0x0c] = array('B', translatedRate.to_bytes(2, byteorder='big'))

				self.PlaybackEntries.append(DTPKPlaybackDataEntry(sampleOffset, sampleEntry))
				print('\tAdded new SPD entry %u (0x%02x) using sample 0x%02x.  DTPK Rate is 0x%x.' % (len(self.PlaybackEntries) - 1, len(self.PlaybackEntries) - 1, curPlayback.targetSample, translatedRate))
			
			self.UpdatePlaybackCount(len(self.PlaybackEntries) - 1)

	def HandleInjectedSamples(self, updatedSamplesList): 
		if (len(updatedSamplesList)):
			print ('\tUpdating sampling rates...')
			warnedOnce = False

			for curUpdatedSample in updatedSamplesList:
				previousRate = INVALID_SAMPLE_ID
				for curPlayback in self.PlaybackEntries:
					if (curPlayback.GetSampleUsed() == curUpdatedSample.samplePosition):
						thisOldRate = curPlayback.GetRate16()
						nUpdatedRate = TranslateDTPKRate(curUpdatedSample.rate)

						if (thisOldRate != nUpdatedRate):
							curPlayback.SetRate(nUpdatedRate)
							print ('\t\tUpdated SPD entry 0x%x at 0x%x to DTPK rate 0x%04x (frequency %u) from 0x%x for sample 0x%x' % (curPlayback.GetPlaybackIDFull(), curPlayback.GetFileAbsolutePlaybackLocation(), nUpdatedRate, TranslateDTPKRate(nUpdatedRate), thisOldRate, curUpdatedSample.samplePosition))
						else:
							print ('\t\tSPD entry 0x%x at 0x%x already at requested DTPK rate 0x%04x (frequency %u) for sample 0x%x' % (curPlayback.GetPlaybackIDFull(), curPlayback.GetFileAbsolutePlaybackLocation(), nUpdatedRate, TranslateDTPKRate(nUpdatedRate), curUpdatedSample.samplePosition))
						if ((previousRate != INVALID_SAMPLE_ID) and (previousRate != thisOldRate) and (not warnedOnce)):
							PrintAsWarning('Warning: this sample is used at multiple rates.  You may want to further adjust the rate value(s) to account for that.')
							warnedOnce = True
						previousRate = thisOldRate

	def Print(self):
		print ('\n  ================PLAYBACK DATA======================')
		if (self.file_offset):
			print ('Sample playback data (64 bytes per sample) starting at 0x%x:' % self.file_offset)
			print ('\tSample playback entry count:\t%u (0x%02x)\t\t\t0x%x' % (len(self.PlaybackEntries), len(self.PlaybackEntries), self.file_offset + 0x10))

			for curPlaybackEntry in self.PlaybackEntries:
				curPlaybackEntry.Print()
		else:
			print ('(No playback data present.)')

class DTPKSampleDataEntry:
	# This parsing logic is based squarely upon https://github.com/Shenmue-Mods/DTPKUtil/blob/master/AM2-DTPK.txt
	# Any mistakes here are mine, any wisdom here is theirs
	def __init__(self, startOffsetChunk, sample_number,  header_chunk, full_sample_chunk):
		self.sample_number = sample_number
		self.def_offset = startOffsetChunk + 4 + (0x10 * sample_number)
		self.header_chunk = header_chunk

		sampleOffset = unpack('<I', self.header_chunk[0:4])[0] & 0x007fffff
		channelsFlag = unpack('<I', self.header_chunk[8:12])[0]
		sample_length = unpack('<I', self.header_chunk[12:16])[0]

		if channelsFlag == 0x80: # double sample count for stereo
			sample_length *= 2

		self.group_id = 0xFFFF
		self.track_id = 0xFFFF
		self.playback_id = 0xFFFF
		self.real_rate = 0

		if (sample_length == len(full_sample_chunk)):
			offsetStartingPos = 0
		else:
			offsetStartingPos = sampleOffset - startOffsetChunk

		self.sample_data = full_sample_chunk[offsetStartingPos:offsetStartingPos + sample_length]

	def IsSampleADPCM(self):
		return unpack('<I', self.header_chunk[0:4])[0] & 0x01000000

	def GetSampleOffset(self):
		return unpack('<I', self.header_chunk[0:4])[0] & 0x007fffff

	def GetSampleQuality(self):
		return unpack('<I', self.header_chunk[0:4])[0] & 0x00800000

	def GetSampleFormat(self):
		return unpack('<I', self.header_chunk[0:4])[0] & 0x01000000

	def GetSampleUnknown(self):
		return unpack('<I', self.header_chunk[0:4])[0] & 0x02000000

	def GetSampleLoopStart(self):
		return unpack('H', self.header_chunk[4:6])[0]
	
	def GetSampleLoopEnd(self):
		return unpack('H', self.header_chunk[6:8])[0]

	def GetSampleChannelsFlag(self):
		return unpack('<I', self.header_chunk[8:12])[0]

	def GetSampleChannelsString(self):
		if self.GetSampleChannelsFlag() == 0x80:
			return "Stereo"
		else:
			return "Mono"

	def GetSampleLengthInSamples(self):
		return unpack('<I', self.header_chunk[12:16])[0]

	def GetSampleLengthInBytes(self):
		nLength = self.GetSampleLengthInSamples()
		if self.GetSampleChannelsFlag() == 0x80: # stereo
			nLength *= 2

		return nLength

	def AssignUsageValues(self, group_id, track_id, actual_rate, playback_id):
		# These are grabbed from other chunks and are not present in the sample data
		self.group_id = group_id
		self.track_id = track_id
		self.playback_id = playback_id
		self.real_rate = actual_rate

	def GetFlagValueForNewLocation(self, location):
		if ((self.GetSampleFormat() == 0x1000000) and (self.GetSampleQuality() == 0)):
			return location | 0x1000000 | 0x2000000
		elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0)):
			return location
		elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0x800000)):
			return location | 0x0800000
		else:
			PrintAsFailure('ERROR: Unknown sample type.')

	def AssignNewSampleOffset(self, definition_offset, sample_offset):
		existingSampleStartingOffset = self.GetSampleOffset() 
		if (existingSampleStartingOffset != sample_offset):
			print ('\tAdjusting sample definition entry at 0x%x for sample location 0x%x length 0x%x (sample was at 0x%x)' % (self.def_offset, sample_offset, self.GetSampleLengthInBytes(), existingSampleStartingOffset))
			self.header_chunk[0:4] = array('B', self.GetFlagValueForNewLocation(sample_offset).to_bytes(4, byteorder='little'))

	def Print(self):
		if (show_detailed_data_samples_full):
			print('\tSample %2u (0x%02x):\t\t\t\t\t\t0x%x' % (self.sample_number, self.sample_number, self.def_offset))
			print('\t\tLocation:\t0x%05x' % self.GetSampleOffset())

			if ((self.GetSampleFormat() == 0x1000000) and (self.GetSampleQuality() == 0)):
				print ('\t\tsample_format:  ADPCM  (0x1000000)')
				print ('\t\tsample_format:  4bit   (0x0000000)')
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0)):
				print ('\t\tsample_format:  PCM    (0x0000000)')
				print ('\t\tsample_quality: 16bit  (0x0000000)')
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0x800000)):
				print ('\t\tsample_format:  PCM    (0x0000000)')
				print ('\t\tsample_quality: 8bit   (0x0800000)')
			else:
				PrintAsFailure('ERROR!!! This is not a valid audio format for the AICA')
				print ('\t\tsample_format:  Unknown (0x%07x)' % self.GetSampleFormat())
				print ('\t\tsample_quality: Unknown (0x%07x)' % self.GetSampleQuality())

			print ('\t\tsample_unk:     0x%07x' % self.GetSampleUnknown())

			print ('\t\tloop_start:     0x%04x count samples' % self.GetSampleLoopStart())
			print ('\t\tloop_end:       0x%04x count samples' % self.GetSampleLoopEnd())
			print ('\t\tchannel_flag:   0x%02x (%s)' % (self.GetSampleChannelsFlag(), self.GetSampleChannelsString()))
			print ('\t\tsample_length:  0x%08x bytes' % self.GetSampleLengthInSamples())
			print ('\t\t\t(channel 1 sample data is at 0x%x to 0x%x)' % (self.GetSampleOffset(), self.GetSampleOffset() + self.GetSampleLengthInSamples()))
			if self.GetSampleChannelsFlag() == 0x80:
				print ('\t\t\t(channel 2 sample data is at 0x%x to 0x%x)' % (self.GetSampleOffset() + self.GetSampleLengthInSamples(), self.GetSampleOffset() + self.GetSampleLengthInBytes()))
		else:
			print ('\tSample %02u (def at 0x%x)' % (self.sample_number, self.def_offset))

# ==============================================================================================
# This ADPCM->PCM region is thanks to Kode54, BERO, Megan Potter, and Sappharad.
# Sappharad's work is under the MIT License as included above.
# BERO is under GPL.
# Kode54 is under MAME GPL v2 or similar
# I'm unclear as to the licensing for KallistiOS and/or Megan Potter's work.
# This section is entirely a straight Python-ization of Sappharad's C# adpcm2wav code
# There is no new value or knowledge added here, it's just going to produce byte-identical
# output to that C# version.
# This is for the ADPCM variant used for the Yamaha AICA ARM7
# See also:
# https://github.com/Sappharad/AicaADPCM2WAV
# https://gitlab.com/simulant/community/lxdream-nitro/-/blob/master/src/aica/audio.c
# https://github.com/mamedev/mame/blob/master/src/devices/sound/aica.cpp
# https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/adpcm.c
# https://github.com/superctr/adpcm/blob/master/ymz_codec.c

#region AICA ADPCM decoding
	def adpcm2pcm(self, file_data, src, length):
		diff_lookup = array('i', [ 1, 3, 5, 7, 9, 11, 13, 15, -1, -3, -5, -7, -9, -11, -13, -15 ])
		index_scale = array('i', [ 0x0e6, 0x0e6, 0x0e6, 0x0e6, 0x133, 0x199, 0x200, 0x266 ])

		dst = bytearray(length * 4)
		dstLoc = 0
		cur_quant = 0x7f
		cur_sample = 0
		highNybble = False
		useSappharad = True

		while (dstLoc < len(dst)):
			if (highNybble):
				shift1 = 4
			else:
				shift1 = 0
			delta = (file_data[src] >> shift1) & 0xf

			# ~2014 mame version by Kode54 / Metallic.  In testing Sapphard's appears to be more accurate for our needs.
			if useSappharad:
				x = cur_quant * diff_lookup[delta & 15]
				x = int(cur_sample + ((x + (self.signedToUnsigned(x, 4) >> 29)) >> 3))
			else:
				x = (cur_quant * diff_lookup[delta & 7]) / 8
				x = min(x, 0x7fff)
				if (delta & 8):
					x = -x

			# standard cleanup
			cur_sample = min(max(x, -32768), 32767)
			cur_quant = (cur_quant * index_scale[delta & 7]) >> 8
			cur_quant = min(max(cur_quant, 0x7F), 0x6000)

			# standard byte packing
			dst[dstLoc] = (int(cur_sample) & 0xFF)
			dstLoc += 1
			dst[dstLoc] = ((int(cur_sample) >> 8) & 0xFF)
			dstLoc += 1

			if useSappharad:
				cur_sample = int(cur_sample * 254 / 256)

			highNybble = not highNybble
			if (not highNybble):
				src += 1

		return dst
	
#endregion

	def ExtractIfYADPCM(self, filename_base):
		if ((self.GetSampleFormat() == 0x1000000) and (self.GetSampleQuality() == 0)):
			filename_out = self.GetFilename(filename_base, "yadpcm")
			sample_data = self.sample_data[0:self.GetSampleLengthInBytes()]

			try:
				open(filename_out, 'wb').write(sample_data)
				print ('ADPCM export: %s' % filename_out)
				return 1
			except:
				PrintAsFailure('ERROR: filename \'%s\' cannot be written to currently.' % filename_out)

			return 1
		return 0

	def InterleaveStereoData(self, chunkData, fIs8Bit):
		swapBuffer = [0x00] * len(chunkData)

		if fIs8Bit:
			remnant = len(chunkData) % 2
			stereoPoint = int((len(chunkData) - remnant) / 2)

			iPos = 0
			iDest = 0
			while iPos < (stereoPoint - 1):
				swapBuffer[0 + iDest] = chunkData[iPos]
				swapBuffer[1 + iDest] = chunkData[iPos + stereoPoint]
				iPos += 1
				iDest += 2

		else:
			remnant = len(chunkData) % 4
			stereoPoint = int((len(chunkData) - remnant)/ 2)

			iPos = 0
			iDest = 0
			while iPos < stereoPoint:
				swapBuffer[0 + iDest] = chunkData[iPos]
				swapBuffer[1 + iDest] = chunkData[iPos + 1]
				swapBuffer[2 + iDest] = chunkData[iPos + stereoPoint]
				swapBuffer[3 + iDest] = chunkData[iPos + 1 + stereoPoint]
				iPos += 2
				iDest += 4

		return swapBuffer

	def ExtractPCMToAIFF(self, filename_base):
		if (self.GetSampleFormat() == 0x0000000) or (dump_adpcm_samples_to_aiff):
			filename_out = self.GetFilename(filename_base, "aiff")

			aiffFile = aifc.open(filename_out, 'wb')

			nChannels = 1
			if self.GetSampleChannelsFlag() == 0x80:
					nChannels = 2

			aiffFile.setnchannels(nChannels)

			if ((self.GetSampleFormat() == 0x1000000) and (self.GetSampleQuality() == 0)):
				# We convert ADPCM 4bit to PCM 16bit for export
				aiffFile.setsampwidth(2)
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0)):
				# PCM 16bit
				aiffFile.setsampwidth(2)
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0x800000)):
				# PCM 8bit
				aiffFile.setsampwidth(1)
			else:
				raise ValueError('ERROR: Unknown sample format specified.')

			frequency = TranslateDTPKRate(self.real_rate)

			aiffFile.setframerate(frequency)

			# Note that there is no support for NAME AUTH chunks so we'll just ignore that

			nLoopStart = self.GetSampleLoopStart()
			if nLoopStart:
				# this is written to the end of file by the aifc lib
				aiffFile.setmark(1, nLoopStart, b'beg loop')
				aiffFile.setmark(2, self.GetSampleLoopEnd(), b'end loop')

			fWasYADPCM = False
			if (self.GetSampleFormat() == 0x1000000):
				fWasYADPCM = True
				chunkDataData = self.adpcm2pcm(self.sample_data, 0, self.GetSampleLengthInBytes())
			else:
				chunkDataData = self.sample_data[0:self.GetSampleLengthInBytes()]

			# needs to be big endian for AIFF 16bit
			if self.GetSampleQuality() != 0x800000:
				iPos = 0
				swapLength = len(chunkDataData) - (len(chunkDataData) % 2)
				while iPos < swapLength:
					tmpVal = chunkDataData[iPos]
					chunkDataData[iPos] = chunkDataData[iPos + 1]
					chunkDataData[iPos + 1] = tmpVal
					iPos += 2

			if self.GetSampleChannelsFlag() == 0x80:
				chunkDataData = array('B', self.InterleaveStereoData(chunkDataData, self.GetSampleQuality() == 0x800000))

			aiffFile.writeframes(chunkDataData)

			aiffFile.close()

			if (fWasYADPCM):
				print ('AIFF export: %s\t(from ADPCM)' % filename_out)
			else:
				print ('AIFF export: %s' % filename_out)
			return 1
		return 0


	def ExtractPCMToWAV(self, filename_base):
		# Note that this sketches in nonPCM export, but that won't work because we don't have a VFW decoder
		# The Shenmue team at https://github.com/Shenmue-Mods/DTPKUtil/blob/master/DTPKutil/DtpkFile.cs
		# has a functional adpcmTopcm converter
		# VGMSteam's code at https://github.com/vgmstream/vgmstream/blob/master/src/coding/yamaha_decoder.c
		# is also interesting
		if (self.GetSampleFormat() == 0x0000000) or (dump_adpcm_samples_to_wav):
			filename_out = self.GetFilename(filename_base, "wav")

			riffHeader = b'RIFF'
			riffType = b'WAVE'
			chunkFmtName = b'fmt '

			# 2b formattag
			# So uh MSFT's formattag registration tables are a little screwy.  In theory
			# the correct formattag might be 0n20 / 0x14 for Yamaha ADPCM, but I haven't
			# seen a VFW decoder that works so who actually knows.  But hey: use 0n20 if 
			# want to be exporting ADPCM WAV
			# PCM == 0x01
			wFormatTag = b'\x01\x00'

			# 2b number of channels
			if self.GetSampleChannelsFlag() == 0x80:
				nChannels = b'\x02\x00'
			else:
				nChannels = b'\x01\x00'

			# 4b sample rate: not stored with samples. instead use the DTPK playback rate value since that is the only thing the user can hear
			frequency = TranslateDTPKRate(self.real_rate)

			# 4b bytes per second
			if self.GetSampleChannelsFlag() == 0x80:
				# stereo
				nSamplesPerSec = frequency.to_bytes(4, byteorder='little')
				nAvgBytesPerSec = (frequency * 2).to_bytes(4, byteorder='little')
			else:
				nSamplesPerSec = nAvgBytesPerSec = frequency.to_bytes(4, byteorder='little')

			# 2b block align
			nBlockAlign = b'\x02\x00'
			# 2b bits per sample

			fIsHalfQuality = False
			if ((self.GetSampleFormat() == 0x1000000) and (self.GetSampleQuality() == 0)):
				# We convert ADPCM 4bit to PCM 16bit for export
				nBitsPerSample = b'\x10\x00'
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0)):
				# PCM 16bit
				nBitsPerSample = b'\x10\x00'
			elif ((self.GetSampleFormat() == 0) and (self.GetSampleQuality() == 0x800000)):
				# PCM 8bit
				nBitsPerSample = b'\x08\x00'
				fIsHalfQuality = True
			else:
				raise ValueError('ERROR: Unknown sample format specified.')

			exData = b''

			# You only need this if you're exporting out ADPCM-as-ADPCM within WAV.  But again: no VFW decoder
			# for that that I'm aware of
			#if (self.format == 0x1000000): #ADPCM
			#	exData = b'\x16\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' 

			chunkFmtData = wFormatTag + nChannels + nSamplesPerSec + nAvgBytesPerSec + nBlockAlign + nBitsPerSample + exData

			chunkFmtSize = pack('l', len(chunkFmtData))

			chunkDataName = b'data'

			fWasYADPCM = False
			if (self.GetSampleFormat() == 0x1000000):
				fWasYADPCM = True
				chunkDataData = self.adpcm2pcm(self.sample_data, 0, self.GetSampleLengthInBytes())
			else:
				chunkDataData = self.sample_data[0:self.GetSampleLengthInBytes()]

			if fIsHalfQuality:
				# Xor the data here and I don't know why
				for iPos, iCurr in enumerate(chunkDataData):
					chunkDataData[iPos] = chunkDataData[iPos] ^ 0x80

			if self.GetSampleChannelsFlag() == 0x80:
				chunkDataData = array('B', self.InterleaveStereoData(chunkDataData, self.GetSampleQuality() == 0x800000))

			chunkDataSize = pack('l', len(chunkDataData))

			riffBody = riffType + chunkFmtName + chunkFmtSize + chunkFmtData + chunkDataName + chunkDataSize + chunkDataData

			fileSize = pack('l', len(riffBody))

			try:
				open(filename_out, 'wb').write(riffHeader + fileSize + riffBody)
				if (fWasYADPCM):
					print ('WAV export: %s\t(from ADPCM)' % filename_out)
				else:
					print ('WAV export: %s' % filename_out)
				return 1
			except:
				PrintAsFailure('ERROR: filename \'%s\' cannot be written to currently.' % filename_out)
		return 0

	def GetFilename(self, filename_base, extension):
		if (self.group_id == 0xffff):
			strGroupId = "NA"
		else:
			strGroupId = '%02u' % self.group_id

		if (self.track_id == 0xffff):
			strTrackId = "NA"
		else:
			strTrackId = '%02u' % self.track_id

		if (self.playback_id == 0xffff):
			strPlaybackId = "NA"
		else:
			strPlaybackId = '%02u' % self.playback_id

		#create file
		filename_out = ('%s_%s_%s_SPD_%s_Sample_%02X_Rate_%02X.%s' % (filename_base, strGroupId, strTrackId, strPlaybackId, self.sample_number, self.real_rate, extension))

		return filename_out

	# This needed utility code comes from hl037_ on stackoverflow
	def signedToUnsigned(self, n, byte_count): 
		return int.from_bytes(n.to_bytes(byte_count, 'little', signed=True), 'little', signed=False)

class DTPKSampleDataChunk:
	def __init__(self, file_offset, data_chunk):
		self.file_offset = 0
		self.SampleEntries = []

		if len(data_chunk):
			self.file_offset = file_offset
			countSamples = unpack('<I', data_chunk[0:4])[0] + 1

			data_offset = 4
			for iSample in range(countSamples):
				self.SampleEntries.append(DTPKSampleDataEntry(self.file_offset, iSample, data_chunk[data_offset:data_offset + 0x10], data_chunk))
				data_offset += 0x10

	def GetCountADPCMSamples(self):
		nTotalCount = 0
		for curEntry in self.SampleEntries:
			if (curEntry.IsSampleADPCM()):
				nTotalCount +=1
		return nTotalCount

	def GetCountPCMSamples(self):
		nTotalCount = 0
		for curEntry in self.SampleEntries:
			if (not curEntry.IsSampleADPCM()):
				nTotalCount +=1
		return nTotalCount

	def ExtractIfYADPCM(self, filename_base):
		nTotalCount = 0
		for curEntry in self.SampleEntries:
			nTotalCount += curEntry.ExtractIfYADPCM(filename_base)
		return nTotalCount

	def ExtractPCMToAIFF(self, filename_base):
		nTotalCount = 0
		for curEntry in self.SampleEntries:
			nTotalCount += curEntry.ExtractPCMToAIFF(filename_base)
		return nTotalCount

	def ExtractPCMToWAV(self, filename_base):
		nTotalCount = 0
		for curEntry in self.SampleEntries:
			nTotalCount += curEntry.ExtractPCMToWAV(filename_base)
		return nTotalCount

	def AssignFirstSequencerEntries(self, chunkSequencer, chunkPlaybackData):
		actual_rate = 0
		playback_id = 0xFFFF
		track_id = 0xFFFF
		group_id = 0xFFFF

		if len(chunkPlaybackData.PlaybackEntries):
			for curSampleEntry in self.SampleEntries:
				for curPlayback in chunkPlaybackData.PlaybackEntries:
					if (curPlayback.GetSampleUsed() == curSampleEntry.sample_number):
						actual_rate = curPlayback.GetRate16()
						playback_id = curPlayback.GetPlaybackIDFull()
						group_id, track_id = chunkSequencer.FindSequencerEntry(playback_id)
						break

				# Note that track_id might be 0xffff here if the file uses SONG or if we don't understand the file structure
				curSampleEntry.AssignUsageValues(group_id, track_id, actual_rate, playback_id)

	def HandleInjectedSamples(self, updatedSamplesList):
		sizeOld = 0
		for curUpdatedSample in updatedSamplesList:
			print ('Changing sample %u (0x%x) to %s len 0x%x' % (curUpdatedSample.samplePosition, curUpdatedSample.samplePosition, curUpdatedSample.sampleType, len(curUpdatedSample.dataObject)))
			curDefinitionOffset = self.file_offset + 4 + (0x10 * curUpdatedSample.samplePosition)
			print ('\tRebuilding sample definition entry at 0x%x' % curDefinitionOffset)

			if (curUpdatedSample.samplePosition < len(self.SampleEntries)):
				sizeOld = self.SampleEntries[curUpdatedSample.samplePosition].GetSampleLengthInBytes()
				# Note that the GetHeader call here takes an offset value that we're likely to reset during GetBytes anyways
				self.SampleEntries[curUpdatedSample.samplePosition].header_chunk = array('B', curUpdatedSample.GetHeader(self.SampleEntries[curUpdatedSample.samplePosition].GetSampleOffset()))
				self.SampleEntries[curUpdatedSample.samplePosition].sample_data = array('B', curUpdatedSample.dataObject)
				print ('\tSample injected inline at 0x%x to 0x%x, size change of %u' % (self.SampleEntries[curUpdatedSample.samplePosition].GetSampleOffset(), self.SampleEntries[curUpdatedSample.samplePosition].GetSampleOffset() + len(curUpdatedSample.dataObject), len(curUpdatedSample.dataObject) - sizeOld))
			elif (curUpdatedSample.samplePosition == len(self.SampleEntries)):
				# The new add an entry
				if (len(self.SampleEntries) == 0):
					suggestedOffset = curDefinitionOffset
				else:
					suggestedOffset = self.SampleEntries[curUpdatedSample.samplePosition - 1].GetSampleOffset() + self.SampleEntries[curUpdatedSample.samplePosition - 1].GetSampleLengthInBytes()

				self.SampleEntries.append(DTPKSampleDataEntry(curDefinitionOffset, curUpdatedSample.samplePosition, array('B', curUpdatedSample.GetHeader(suggestedOffset)), array('B', curUpdatedSample.dataObject)))
				print ('\tSample added inline at 0x%x to 0x%x, size change of %u' % (self.SampleEntries[curUpdatedSample.samplePosition].GetSampleOffset(), self.SampleEntries[curUpdatedSample.samplePosition].GetSampleOffset() + len(curUpdatedSample.dataObject), self.SampleEntries[curUpdatedSample.samplePosition].GetSampleLengthInBytes()))
			else:
				PrintAsFailure('ERROR: Samples must be injected sequentially (position request is %u (0x%x), should be a maximum of %u (0x%x)).' % (curUpdatedSample.samplePosition, curUpdatedSample.samplePosition, len(self.SampleEntries), len(self.SampleEntries)))
				
	def GetBytes(self, currentChunkStartingOffset):
		nCountSamples = len(self.SampleEntries)
		nCountSamples -= 1
		outBytes = array('B', nCountSamples.to_bytes(4, byteorder='little'))

		definition_offset = currentChunkStartingOffset + 4
		sampleStartingOffset = definition_offset + (0x10 * len(self.SampleEntries))

		for curSampleEntry in self.SampleEntries:
			curSampleEntry.AssignNewSampleOffset(definition_offset, sampleStartingOffset)
			outBytes += curSampleEntry.header_chunk
			definition_offset += 0x10
			sampleStartingOffset += curSampleEntry.GetSampleLengthInBytes()

		for curSampleEntry in self.SampleEntries:
			outBytes += curSampleEntry.sample_data

		# Handle padding
		remnant = len(outBytes) % 4
		if (remnant != 0):
			outBytes += array('B', bytes(remnant))

		return outBytes

	def Print(self):
		print ('\n  ===============SAMPLE METADATA=====================')
		if self.SampleEntries:
			print ('Sample metadata:')
			print ('\tSample count:\t\t%2u (0x%02x)\t\t\t\t0x%x' % (len(self.SampleEntries), len(self.SampleEntries), self.file_offset))

			for curEntry in self.SampleEntries:
				curEntry.Print()
		else:
			print ('(No sample data present.)')

class DTPKEffectsChunk:
	# Thanks to Vincent for figuring this out!
	# At the start of the Effects chunk is the 4 byte header.  That is the number of effect groups.
	# Each effect group chunk has a 0x24 byte header describing PAN and VOLUME values for each channel.
		# PAN values:
		# 0x00 = Center
		# 0x01-0xF = Right Pan 1-15
		# 0x11-0x1F = Left Pan 1-15

		# VOLUME values:
		# 0x00-0x0F = Volume 0-15
	# This is followed by the .FPD chunk which is 0xc00 bytes.  That chunk handles reverb, fades, chorus, pitch, etc.

	def __init__(self, file_offset, data_chunk):
		self.file_offset = file_offset
		self.data_chunk = data_chunk

	def GetBytes(self):
		return self.data_chunk

	def GetStringForPanValue(self, value):
		if (value == 0x00):
			return "Center"
		elif (value & 0x10):
			return "Left %2u" % (value & 0x0f)
		else:
			return "Right %2u" % value

	def GetEffectGroupsCount(self):
		return unpack('<I', self.data_chunk[0x00:0x04])[0] + 1

	def Print(self):
		print ('\n  ================EFFECTS DATA=======================')
		if (self.data_chunk):
			print ('Effects group count:\t\t%2u (0x%02x)\t\t\t\t0x%x' % (self.GetEffectGroupsCount(), self.GetEffectGroupsCount(), self.file_offset))

			nCurrentOffset = 4
			for iCurGroup in range(self.GetEffectGroupsCount()):
				print ('\tEffects group 0x%02x\t\t\t\t\t\t0x%x' % (iCurGroup, self.file_offset + nCurrentOffset))
				for iCurChannel in range(18):
					bytePan = (self.data_chunk[nCurrentOffset:nCurrentOffset + 1])[0]
					byteVolume = (self.data_chunk[nCurrentOffset + 1:nCurrentOffset + 2])[0]

					if ((bytePan > 0x1f) or (byteVolume > 0x0f)):
						PrintAsFailure('ERROR: Unknown effects value present at 0x%x' % (self.file_offset + nCurrentOffset))

					print ('\t\tChannel %2u: PAN %8s, Volume: %2u (0x%02x, 0x%02x)\t0x%x' % (iCurChannel, self.GetStringForPanValue(bytePan), byteVolume, bytePan, byteVolume, self.file_offset +nCurrentOffset))
					nCurrentOffset += 2
				nCurrentOffset += 0xc00
		else:
			print ('(No effects data present.)')

class DTPKCombinationChunk:
	def __init__(self, data_chunk):
		self.data_chunk = data_chunk

	def GetBytes(self):
		return self.data_chunk

	def Print(self, chunkFileOffset):
		if (len(self.data_chunk)):
			entityCount = self.data_chunk[0:2][0] + 1
			entityOffsets = []
			for iCurOffset in range(entityCount):
				entityOffsets.append(unpack('<h', self.data_chunk[2 + (iCurOffset * 2):4 + (iCurOffset * 2)])[0])

			print ('\n  =================COMBINATION DATA==================')
			currentFileOffset = chunkFileOffset
			print ('Combination count:\t0x%02x\t\t\t0x%03x' % (entityCount, chunkFileOffset))
			
			if show_detailed_combination_data:
				for iCount, currentDataOffset in enumerate(entityOffsets):
					# Chunks are padded to 4 byte alignment
					# 8 byte section header
					# byte 00 timbre entry count: 0-15
					# byte 01 is 0-127 volume
					print ('\tCombination 0x%02x:\t\t\t0x%03x' % (iCount, chunkFileOffset + currentDataOffset))
					countTimbres = self.data_chunk[currentDataOffset:currentDataOffset + 1][0] + 1
					print ('\t\tTimbre count:\t0x%02x' % (countTimbres))
					currentDataOffset += 1
					print ('\t\tGeneral volume:\t0x%02x' % (self.data_chunk[currentDataOffset:currentDataOffset + 1][0]))
					currentDataOffset += 7

					# each Timbre 1-8 section is 0x20 bytes long
					# byte 00	80 == Active, 00 == Mute
						# Note that the SOLO option simply mutes the other timbres
					# byte 01	MIDI to use (1-16)
					# byte 02	00
					# byte 03	00
					# byte 04	7f?
					# byte 05	00
					# byte 06	7f?
					# byte 07	FX channel << 8 | FX snd: values 0-0xF of course
					# byte 08   Range 0-0x0f
					# byte 09	Volume 0-0x7f
					# byte 0a	Pan: 0x00 center, 0x10-0xF: right, 0x10-0x1f left
					# byte 0b	Trans: signed 0xc0-0x3f?
					# byte 0c	Tune: signed 0xc0-0x3f
					# byte 0d-1f 00 Unused

					for curTimbreEntry in range(countTimbres):
						print ('\t\t\tTimbre 0x%02x:\t\t0x%03x' % (curTimbreEntry, chunkFileOffset + currentDataOffset))
						nActiveValue = self.data_chunk[currentDataOffset + 0x0:currentDataOffset + 0x1][0]
						nMIDIToUse   = self.data_chunk[currentDataOffset + 0x1:currentDataOffset + 0x2][0] + 1
						nFXValues    = self.data_chunk[currentDataOffset + 0x8:currentDataOffset + 0x9][0]
						nRange       = self.data_chunk[currentDataOffset + 0x9:currentDataOffset + 0xa][0]
						nVolume      = self.data_chunk[currentDataOffset + 0xa:currentDataOffset + 0xb][0]
						nPan         = self.data_chunk[currentDataOffset + 0xb:currentDataOffset + 0xc][0]
						nTrans       = self.data_chunk[currentDataOffset + 0xc:currentDataOffset + 0xd][0]
						nTune        = self.data_chunk[currentDataOffset + 0xd:currentDataOffset + 0xe][0]

						if (nActiveValue & 0x80):
							strUsage = "Active"
						else:
							strUsage = "Muted"
						nFXSnd     = (nFXValues & 0xF0) >> 4
						nFXChannel = nFXValues & 0xF
						if nPan == 0x00:
							strPan = "Center"
						elif nPan >= 0x10:
							strPan = "Left: %u" % (nPan & 0x0f)
						else:
							strPan = "Right: %u" % (nPan & 0x0f)
						if nTrans > 64:
							nTrans = nTrans - 0x100
						if nTune > 64:
							nTune = nTune - 0x100

						print ('\t\t\t  Usage:\t%s' % (strUsage))
						print ('\t\t\t  MIDI used:\t%u' % (nMIDIToUse))
						print ('\t\t\t  FX Channel:\t%u' % (nFXChannel))
						print ('\t\t\t  FX Snd:\t%u' % (nFXSnd))
						print ('\t\t\t  Range:\t%u' % (nRange))
						print ('\t\t\t  Volume:\t%u' % (nVolume))
						print ('\t\t\t  Pan:\t\t%s' % (strPan))
						print ('\t\t\t  Trans:\t%i' % (nTrans))
						print ('\t\t\t  Tune:\t\t%i' % (nTune))
						currentDataOffset += 0x20

class DTPKProgramDefsChunk:
	def __init__(self, data_chunk):
		self.data_chunk = data_chunk

	def GetBytes(self):
		return self.data_chunk

	def Print(self, chunkFileOffset):
		if (len(self.data_chunk)):
			print ('\n  =============PROGRAM DEFINITIONS DATA==============' )
			internalDataOffset = (unpack('<h', self.data_chunk[2:4]))[0]
			entityCount = (unpack('<h', self.data_chunk[internalDataOffset:internalDataOffset + 2]))[0] + 1
			print ('Program count:\t\t0x%02x\t\t\t0x%03x' % (entityCount , chunkFileOffset))
			entityOffsets = []
			for curEntity in range(entityCount):
				entityOffsets.append((unpack('<h', self.data_chunk[internalDataOffset + 2 + (2 * curEntity):internalDataOffset + 4 + (2 * curEntity)]))[0])

			for iPos, curOffset in enumerate(entityOffsets):
				print ('\tProgram definition %u data at: 0x%03x' % (iPos, chunkFileOffset + curOffset + 4))

class DTPKUnhandledChunk:
	def __init__(self, data_chunk):
		self.data_chunk = data_chunk

	def GetBytes(self):
		return self.data_chunk

	def Print(self, nIndex, chunkFileOffset):
		if (len(self.data_chunk)):
			print ('\n  ==================UNKNOWN DATA %u===================' % (nIndex))
			chunkCRC = zlib.crc32(self.data_chunk)
			print ('Unknown Chunk %u crc32:\t0x%02x\t\t0x%03x' % (nIndex, chunkCRC, chunkFileOffset))

class DTPKRegions(Enum):
	Header      = 0
	Combination = 1
	Program     = 2
	Unknown     = 3
	Sequencer   = 4
	Playback    = 5
	ICS         = 6
	Effects     = 7
	Samples     = 8
	EOF         = 9

class DTPKHeaderChunk:
	def __init__(self, data_chunk):
		self.data_chunk = data_chunk
		if (len(data_chunk) < 0x40):
			PrintAsFailure('ERROR: The header chunk is 0x%x bytes long.  It must be at least 0x40 bytes.' % (len(data_chunk)))
			sys.exit(0)

	def GetBytes(self):
		return self.data_chunk

	def GetDTPKId(self):
		return unpack('<I', self.data_chunk[0x04:0x08])[0]

	def GetFileSize(self):
		return unpack('<I', self.data_chunk[0x08:0x0c])[0]

	def WarnIfFileSizeValueIsWrong(self, filesize_actual):
	# Note we may adjust this size for write-out if we splice in samples
		if (self.GetFileSize() != filesize_actual):
			# This will cause unexpected errors, so mention it to the user.
			PrintAsFailure('ERROR! Filesize in file is listed as 0x%x, but actual filesize is 0x%x.  This file may not play back correctly.' % (self.GetFileSize(), filesize_actual))

	def GetChunkOffsetForRegion(self, region):
		if   region == DTPKRegions.Header:
			return 0
		elif region == DTPKRegions.Combination:
			return unpack('<I', self.data_chunk[0x20:0x24])[0]
		elif region == DTPKRegions.Program:
			return unpack('<I', self.data_chunk[0x24:0x28])[0]
		elif region == DTPKRegions.Unknown:
			# Note that in the data chunk the first four bytes are count entries
			# Then there is an 0x80 byte chunk per entry
			return unpack('<I', self.data_chunk[0x28:0x2c])[0]
		elif region == DTPKRegions.Sequencer:
			return unpack('<I', self.data_chunk[0x2c:0x30])[0]
		elif region == DTPKRegions.Playback:
			return unpack('<I', self.data_chunk[0x30:0x34])[0]
		elif region == DTPKRegions.ICS:
			# The Intelligent Control Sound chunk starts with count and then the ICS entries
			# Each ICS entry is a minimum of 0x04 bytes
			return unpack('<I', self.data_chunk[0x34:0x38])[0]
		elif region == DTPKRegions.Effects:
			return unpack('<I', self.data_chunk[0x38:0x3c])[0]
		elif region == DTPKRegions.Samples:
			return unpack('<I', self.data_chunk[0x3c:0x40])[0]
		elif region == DTPKRegions.EOF:
			return unpack('<I', self.data_chunk[0x08:0x0c])[0]
		else:
			PrintAsFailure('ERROR: Unsupported region.')
			return 0

	def GetNextChunkOffsetAfterRegion(self, region, startOffset):
		# Handle shuffled chunks.  I've only seen shuffled chunks in corrupt DTPKs,
		# buuuuut it's something we can easily account for.
		# Worst case scenario is chunk terminates at EOF
		lowNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.EOF)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Combination)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Program)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Unknown)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Sequencer)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Playback)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.ICS)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Effects)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)
		correctedNextOffset = self.GetChunkOffsetForRegion(DTPKRegions.Samples)
		if correctedNextOffset > startOffset:
			lowNextOffset = min(lowNextOffset, correctedNextOffset)

		return lowNextOffset


	def GetChunkLengthForRegion(self, region):
		startOffset = self.GetChunkOffsetForRegion(region)
		if startOffset == 0 and region != DTPKRegions.Header:
			return 0

		endOffset = self.GetNextChunkOffsetAfterRegion(region, startOffset)
		return endOffset - startOffset

	def Print(self):
		print ('\n  ====================HEADER DATA====================')
		print ('Signature:\t\tDTPK\t\t\t0x00')
		print ('DTPK id:\t\t0x%02x\t\t\t0x04' % self.GetDTPKId())
		print ('Filesize:\t\t0x%x (0n%u)\t0x08' % (self.GetFileSize(), self.GetFileSize()))
		print ('Header size:\t\t\t\t\t\tLength: 0x%x bytes' % (self.GetChunkLengthForRegion(DTPKRegions.Header)))
		print ('Combination chunk at:\t0x%x\t\t\t0x20\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Combination), self.GetChunkLengthForRegion(DTPKRegions.Combination)))
		print ('Program Defs chunk at:\t0x%x\t\t\t0x24\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Program), self.GetChunkLengthForRegion(DTPKRegions.Program)))
		print ('Unknown chunk at:\t0x%x\t\t\t0x28\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Unknown), self.GetChunkLengthForRegion(DTPKRegions.Unknown)))
		print ('Seq data at:\t\t0x%x\t\t\t0x2c\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Sequencer), self.GetChunkLengthForRegion(DTPKRegions.Sequencer)))
		print ('Playback data at:\t0x%x\t\t\t0x30\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Playback), self.GetChunkLengthForRegion(DTPKRegions.Playback)))
		print ('ICS chunk at:\t\t0x%x\t\t\t0x34\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.ICS), self.GetChunkLengthForRegion(DTPKRegions.ICS)))
		print ('Effects chunk at:\t0x%x\t\t\t0x38\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Effects), self.GetChunkLengthForRegion(DTPKRegions.Effects)))
		print ('Sample data at:\t\t0x%x\t\t\t0x3c\tLength: 0x%x bytes' % (self.GetChunkOffsetForRegion(DTPKRegions.Samples), self.GetChunkLengthForRegion(DTPKRegions.Samples)))

# ==============================================================================================
def IsTrackEntry(entryType):
	if ((entryType == 0xdc) or # seen in cvs2
		(entryType == 0xdd) or # seen in cvs2
		(entryType == 0xde) or # seen in cvs2
		(entryType == 0xdf)):  # seen in mvc2
		return True
	return False

def IsKnownTrackAction(actionType):
	if ((actionType == 0xa0) or # System event
		(actionType == 0xa4) or # FX Channel action
		(actionType == 0xa9)):  # request bank entry N
		return True
	return False

def GetStringForControlSwitchValue(value):
	if   value == 0x00:
		return 'NULL'
	elif value == 0x01:
		return 'All Sound Off'
	elif value == 0x02:
		return 'All Song Off'
	elif value == 0x03:
		return 'All SE Off'
	elif value == 0x04:
		return 'All ICS Off'
	elif value == 0x08:
		return 'Stereo SW'
	elif value == 0x09:
		return 'Mono SW'
	elif value == 0x0a:
		return 'MIDI Thru On'
	elif value == 0x0b:
		return 'MIDI Thru Off'
	elif value == 0x0c:
		return 'MIDI Internal'
	elif value == 0x0d:
		return 'MIDI External'
	elif value == 0x0e:
		return 'MIDI Both'
	elif value == 0x0f:
		return 'Request Thru On'
	elif value == 0x10:
		return 'Request Thru Off'
	elif value == 0x11:
		return 'Port Sound Off'
	elif value == 0x12:
		return 'Port Song Off'
	elif value == 0x13:
		return 'Port SE Off'
	else:
		return 'Unknown'

def GetStringForSystemEventType(entry_type, SPD_used_or_event_id, volume_or_event_value):
	summary = 'System Event (0x%02x).  ' % entry_type
	if (SPD_used_or_event_id == 0x00):
		summary += 'Control Switch (0x%02x): ' % SPD_used_or_event_id
		# Control Switches have named values
		# EVENT values outside of SONG are masked with 0x80 on disk
		if volume_or_event_value >= 0x80:
			summary += '%s (0x%02x as 0x%02x)' % (GetStringForControlSwitchValue(volume_or_event_value ^ 0x80), volume_or_event_value ^ 0x80, volume_or_event_value)
		else:
			summary += '%s (0x%02x)' % (GetStringForControlSwitchValue(volume_or_event_value), volume_or_event_value)
	else:
		if   (SPD_used_or_event_id == 0x01):
			summary += 'Total Volume (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x02):
			summary += 'Song Pause (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x03):
			summary += 'Song Continue (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x04):
			summary += 'Song Volume (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x05):
			summary += 'Song Volume Bias (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x06):
			summary += 'Song Tempo Bias (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x07):
			summary += 'Song Transpose (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x09):
			summary += 'Fade In (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x0a):
			summary += 'Fade Out (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x10):
			summary += 'SE Volume (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x19):
			summary += 'Combi Change (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x1a):
			summary += 'MIDI Port Change (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x1b):
			summary += 'Hard Volume (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x20):
			summary += 'Sync Level (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x24):
			summary += 'Status Display (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x28):
			summary += 'Silent On (0x%02x).  ' % SPD_used_or_event_id
		elif (SPD_used_or_event_id == 0x29):
			summary += 'Silent Off (0x%02x).  ' % SPD_used_or_event_id
		else:
			PrintAsFailure('ERROR: Unknown event: 0x%02x.  We should look into this file.' % (SPD_used_or_event_id))

		# EVENT values outside of SONG are masked with 0x80 on disk
		if volume_or_event_value >= 0x80:
			summary += 'Value: 0x%02x (as 0x%02x) ' % (volume_or_event_value ^ 0x80, volume_or_event_value)
		else:
			summary += 'Value: 0x%02x ' % (volume_or_event_value)
	return summary

# ==============================================================================================
def TranslateDTPKRate(frequency):
	#DTPK rate value examples can be found in these files:
	# Unknown values (BUGBUG todo)
	# 01 doa fight_se
		#  78/0x4e, sample 0x23 - track 17 join?
		# 120/0x78, sample 0x21 - track 62 join?
		# 123/0x7b, sample 0x16 - track 73 join?
		# 148/0x94, sample 0x29 - track 78 join?
	# 02 (doa2 fight_se km_c2/ks_c2/kt_end/tg_c123): ?  if this is correct, this is unknown.
		# 25/0x19, sample 0x28 - part of track 8
		# 74/0x4a sample 0x29
	# 04 cvs2 SND_PL2A (Hibiki): track 49/0x31, sample 39/0x27, also track 103/0x67
		# confirmed in file
		# seems to be about 44100-45000?
	# 05 doa fight_se
		# 75/0x4b, sample 0x29

	# Unused value...?  Not going to make this the new zero_value because if there is usage of 0xd1-0xdd I'd like to test them first
	# d1 DOA voice_le voice_lu: unused - empty samples

	# "Known" values:
	# 0x0 44100: doa2
	
	# 0xde 6000: mvc2 pl3a kobun/servbot SPD 0x1f sample 0x1a track 39 (slowdown of 38)
	# 0xdf 6500: unused? not seen in content yet
	# 0xe0 7000: unused? not seen in content yet
	# 0xe1 7500: mvc2 pl10_voi tron bonne, spd 0x17, 0x18, 0x41, 0x42 - sample 0x20 0x15, track 30 (slowdown of 29) and 32
	# 0xe2 8000: mvc2 guile 02
	# 0xe3 8500: speculative: needs verification
	# 0xe4 9000: speculative: seems good for mvc2 sakura, abyss
	# 0xe5 9500: speculative: needs verification
	# 0xe6 10000: cvs2 rolento
	# 0xe7 10500: speculative: seems good for mvc2 sakura
	# 0xe8 11025: most
	# 0xe9 12000: cvs2 rolento
	# 0xea 12500: speculative: needs verification
	# 0xeb 13000: speculative: seems good for mvc2 bonerine
	# 0xec 14000: speculative: needs verification
	# 0xed 15000: speculative: needs verification
	# 0xee 16000: speculative: seems good for mvc2 storm, jin
	# 0xef 17000: speculative: seems good for doa2 voice_ba
	# 0xf0 18000: speculative: seems good for mvc2 storm, spiral
	# 0xf1 19000: speculative: seems good for doa2 fight_se/voice_kl, cvs2 guile
	# 0xf2 20000: speculative: seems good for vtennis pack 8
	# 0xf3 21000: speculative: seems good for vtennis pack 8
	# 0xf4 22050: lots
	# 0xf5 23000: cvs2 dhalsim
	# 0xf6 24000: cvs2 vega / vtennis pack 4
	# 0xf7 26000: speculative: seems good for vtennis pack4, doa2 km_c1
	# 0xf8 28000: speculative: seems good for doa2 fight_se
	# 0xf9 30000: speculative: needs verification
	# 0xfa 32000: cvs2 cammy
	# 0xfb 34000: virt tennis pack0
	# 0xfc 35000: speculative: needs verification
	# 0xfd 38000: speculative: seems good for vtennis pack0
	# 0xfe 40000: speculative: seems good for doa2 fight_se
	# 0xff 42000: speculative: seems good for doa2 fight_se

	# Improved knowledge! we now know that rate is stored as a 16bit composite value
	# incorporating base note, transposition, and detune.  we don't know how to cleanly 
	# unscramble that, but here are some confirmed values:
		# 4000  == 0xd61d
		# 6000  == 0xdd1e
		# 6500  == 0xde36
		# 7000  == 0xe008
		# 8000	== 0xe21d
		# 8012	== 0xe21e
		# 8500  == 0xe320
		# 9000  == 0xe41f
		# 9500  == 0xe51c
		# 10500 == 0xe70a
		# 11025 == 0xe800
		# 12000 == 0xe91e
		# 12500 == 0xea0b
		# 13000 == 0xea36
		# 14000 == 0xec08
		# 15000 == 0xed15
		# 16000 == 0xee1d
		# 17000 == 0xef20
		# 18000 == 0xf01f
		# 19000 == 0xf11c
		# 20000 == 0xf214
		# 21000 == 0xf30a
		# 22050 == 0xf400
		# 23000 == 0xf42f

	rateDictionary16bit = { 4000 : 0xd61d, 6000 : 0xdd1e, 6500 : 0xde36, 7000 : 0xe008,
							8000 : 0xe21d, 8012 : 0xe21e, 8500 : 0xe320,
							9000 : 0xe41f, 9500 : 0xe51c, 10500 : 0xe70a,
							11025 : 0xe800, 12000 : 0xe91e, 12500 : 0xea0b,
							13000 : 0xea36, 14000 : 0xec08, 15000 : 0xed15,
							16000 : 0xee1d, 17000 : 0xef20, 18000 : 0xf01f,
							19000 : 0xf11c, 20000 : 0xf214, 21000 : 0xf30a,
							22050 : 0xf400, 23000 : 0xf42f,
							# everything above is real.
							# everything following is speculative!
							24000 : 0xf4f6,
							25000 : 0xf600,
							26000 : 0xf71d,
							28000 : 0xf800,
							30000 : 0xf900,
							32000 : 0xfa13,
							34000 : 0xfb00,
							35000 : 0xfc1d,
							38000 : 0xfd15,
							40000 : 0xfe1d,
							42000 : 0xff00
							}

	rateLowDictionary16bit = {
							44100 : 0x0000,
							45000 : 0x0100,
							46000 : 0x0200,
							47000 : 0x0300,
							48000 : 0x0400,
							49000 : 0x0500
							}

	if (frequency < 0x600): # DTPK low rate table, high frequency
		for stockFrequency, stockDTPKRate in rateLowDictionary16bit.items():
			if frequency == stockDTPKRate:
				return stockFrequency
			elif frequency < stockDTPKRate:
				return stockFrequency
		return 44100
	elif frequency > 0xd000: # DTPK 16bit rate to frequency
		# BUGBUG: warn for close match?
		for stockFrequency, stockDTPKRate in rateDictionary16bit.items():
			if frequency == stockDTPKRate:
				return stockFrequency
			elif frequency < (stockDTPKRate + 0x20):
				# If it's close, run with it
				#PrintAsWarning('Warning: rate \'0x%x\' is not currently mapped for DTPK.  Using \'0x%x\' as closest known match.' % (frequency, stockDTPKRate))
				return stockFrequency
		return 0
	else: # frequency to DTPK rate
		# BUGBUG: warn for close match?
		if (frequency >= 44100):
			for stockFrequency, stockDTPKRate in rateLowDictionary16bit.items():
				if frequency == stockFrequency:
					return stockDTPKRate
				elif frequency < stockFrequency:
					PrintAsWarning('Warning: frequency %u is not currently mapped for DTPK.  Using \'%u\' as closest known match.\n'
									'If you get Preppy this file we can map this more correctly if you need it.'  % (frequency, stockFrequency))
				return stockDTPKRate
		else:
			for stockFrequency, stockDTPKRate in rateDictionary16bit.items():
				if frequency == stockFrequency:
					return stockDTPKRate
				elif frequency < stockFrequency:
					PrintAsWarning('Warning: frequency %u is not currently mapped for DTPK.  Using \'%u\' as closest known match.\n'
									'If you get Preppy this file we can map this more correctly if you need it.' % (frequency, stockFrequency))
					return stockDTPKRate

		PrintAsWarning('Warning: frequency %u is not supported in DTPK.  For best results please resample this to a supported rate.' % (frequency))

		# No remote match: use stock 22050
		return 0xf400

# ==============================================================================================
def TranslateDTPKPacked127Value(packedValue):
		# There should be bitmath to get to this point, but I wasn't getting it exactly
		# right and thus I'm just going to skip that and use a lookup table.
		DTPK127Encoding = [ 0x000, 0x389, 0x392, 0x398, 0x3a4, 0x3ad, 0x3b6, 0x3bf, # 0-7
							0x3c8, 0x3d1, 0x3da, 0x3e3, 0x3ec, 0x3f5, 0x3fe, 0x407, # 8-15
							0x410, 0x419, 0x422, 0x42b, 0x434, 0x43d, 0x446, 0x44f, # 16-23
							0x458, 0x461, 0x46a, 0x473, 0x47c, 0x485, 0x48e, 0x497, # 24-31
							0x4a0, 0x4a9, 0x4b2, 0x4bb, 0x4c4, 0x4cd, 0x4d6, 0x4df, # 32-39
							0x4e8, 0x4f1, 0x4fa, 0x503, 0x50c, 0x515, 0x51e, 0x527, # 40-47
							0x530, 0x539, 0x542, 0x54b, 0x554, 0x55d, 0x566, 0x56f, # 48-55
							0x578, 0x581, 0x58a, 0x593, 0x59c, 0x5a5, 0x5ae, 0x5b7, # 56-63
							0x5c0, 0x5c9, 0x5d2, 0x5db, 0x5e4, 0x5ed, 0x5f6, 0x5ff, # 64-71
							0x608, 0x611, 0x61a, 0x623, 0x62c, 0x635, 0x63e, 0x647, # 72-79
							0x650, 0x659, 0x662, 0x66b, 0x674, 0x67d, 0x686, 0x68f, # 80-87
							0x698, 0x6a1, 0x6aa, 0x6b3, 0x6bc, 0x6c5, 0x6ce, 0x6d7, # 88-95
							0x6e0, 0x6e9, 0x6f2, 0x6fb, 0x704, 0x70d, 0x716, 0x71f, # 96-103
							0x728, 0x731, 0x73a, 0x743, 0x74c, 0x755, 0x75e, 0x767, # 104-111
							0x770, 0x779, 0x782, 0x78b, 0x794, 0x79d, 0x7a6, 0x7af, # 112-119
							0x7b8, 0x7c1, 0x7ca, 0x7d3, 0x7dc, 0x7e5, 0x7ee, 0x7fd  # 120-127
							]

		for iPos in range(len(DTPK127Encoding)):
			if packedValue == DTPK127Encoding[iPos]:
				return iPos

		PrintAsFailure('ERROR: Unknown packed value \'0x%03x\'.' % (packedValue))
		return 0


# ==============================================================================================
class InjectableSampleType(Enum):
	PCM16  = 1
	PCM8   = 2
	ADPCM4 = 3

# ==============================================================================================
class InjectableSample:
	samplePosition = 0
	sampleType = InjectableSampleType.PCM16
	rate = 22050
	dataObject = []

	def __init__(self, bank_position, sample_type, waveData, rate, channelsFlag, loopStart = 0, loopEnd = 0):
		self.samplePosition = bank_position
		self.sampleType = sample_type
		self.dataObject = waveData

		# If we get a DTPK rate value, convert it to the standard playback rate
		if (rate < 6000) or (rate > 49000):
			PrintAsWarning('Warning: the rate value of \'%u\' is out of normal DTPK range and may not play back correctly.' % (rate))

		self.rate = rate
		self.channelsFlag = channelsFlag
		self.loopStart = loopStart
		self.loopEnd = loopEnd

		if channelsFlag == 0x80:
			# Incoming PCM multichannel audio is interleaved: deinterleave since that's
			# how we store it in DTPK
			swapBuffer = [0x00] * len(self.dataObject)

			if self.sampleType == InjectableSampleType.PCM8:
				remnant = len(self.dataObject) % 2
				stereoPoint = int((len(self.dataObject) - remnant)/ 2)

				iPos = 0
				iInsertPoint = 0
				while iPos < (len(self.dataObject) - remnant):
					swapBuffer[iInsertPoint]               = self.dataObject[iPos]
					swapBuffer[iInsertPoint + stereoPoint] = self.dataObject[iPos + 1]
					iInsertPoint += 1
					iPos += 2
			elif self.sampleType == InjectableSampleType.PCM16:
				remnant = len(self.dataObject) % 4
				stereoPoint = int((len(self.dataObject) - remnant)/ 2)

				iPos = 0
				iInsertPoint = 0
				while iPos < (len(self.dataObject) - remnant):
					swapBuffer[iInsertPoint]                   = self.dataObject[iPos + 0]
					swapBuffer[iInsertPoint + 1]               = self.dataObject[iPos + 1]
					swapBuffer[iInsertPoint + stereoPoint]     = self.dataObject[iPos + 2]
					swapBuffer[iInsertPoint + 1 + stereoPoint] = self.dataObject[iPos + 3]
					iInsertPoint += 2
					iPos += 4

			self.dataObject = array('B', swapBuffer)

	def GetHeader(self, location):
		sampleLengthInSamples = len(self.dataObject)
		if (self.channelsFlag == 0x80):
			sampleLengthInSamples = int(sampleLengthInSamples / 2)

		if (self.sampleType == InjectableSampleType.ADPCM4):
			flags = location | 0x1000000 | 0x02000000
			loopEnd = sampleLengthInSamples * 2
			# 28 is a speculative magic number here as seen in cvs2 adpcm samples
			# and as such could be wrong
			loopStart = loopEnd - 28
		elif (self.sampleType == InjectableSampleType.PCM8):
			flags = location | 0x0800000
			loopStart = 0
			loopEnd = sampleLengthInSamples
		elif (self.sampleType == InjectableSampleType.PCM16):
			flags = location
			loopStart = 0
			loopEnd = int(sampleLengthInSamples / 2)
		else:
			raise ValueError('ERROR: Unknown sample type specified.')

		# We might have a valid loopEnd value if we started with an AIFF with one, so use if it exists
		if (self.loopEnd != 0):
			loopEnd = self.loopEnd

		outBytes = bytearray(16)
		outBytes[0:4] = array('B', flags.to_bytes(4, byteorder='little'))
		if self.loopStart or (self.sampleType != InjectableSampleType.ADPCM4):
			# Note that loopStart seems to be required for YADPCM, so we'll force a value for that
			outBytes[4:6] = array('B', self.loopStart.to_bytes(2, byteorder='little'))
		else:
			outBytes[4:6] = array('B', loopStart.to_bytes(2, byteorder='little'))

		if loopEnd > 0xffff:
			PrintAsWarning('Warning: injected audio has too many samples (0x%x) and will overflow the loop value.  Kludging to 0xffff to avoid this. '
							'Consider downsampling this file to prevent issues.' % (loopEnd))
			loopEnd = 0xffff			
			
		outBytes[6:8] = array('B', loopEnd.to_bytes(2, byteorder='little'))
		outBytes[8:12] = array('B', self.channelsFlag.to_bytes(4, byteorder='little'))

		outBytes[12:16] = array('B', sampleLengthInSamples.to_bytes(4, byteorder='little'))
		return outBytes

# ==============================================================================================
def ImportAndAddADPCMFile(adpcmFilename, sampleList, samplePosition, sampleRate, channelsFlag = 0):
	# This is just a raw data chunk
	dataChunk = array('B', open(adpcmFilename, 'rb').read())
	sampleType = InjectableSampleType.ADPCM4
	sampleList.append(InjectableSample(samplePosition, sampleType, dataChunk, sampleRate, channelsFlag))
	return True

# ==============================================================================================
def ImportAndAddAudioFile(fileType, importFilename, sampleList, samplePosition):
	try:
		if fileType == "AIFF":
			audioObject = aifc.open(importFilename, 'rb')
		else:
			audioObject = wave.open(importFilename, 'rb')
	except Exception:
		PrintAsFailure("ERROR: %s cannot be loaded." % (fileType))
		return False

	isValid = True
	try:
		nChannel = audioObject.getnchannels()
		if   nChannel == 1:
			audioChannelsFlag = 0x00
		elif nChannel == 2:
			audioChannelsFlag = 0x80
		else:
			PrintAsFailure('ERROR: Only mono or stereo %s is supported.' % (fileType))
			raise ValueError('Invalid number of channels')

		nSampleWidth = audioObject.getsampwidth()
		if (nSampleWidth == 1):
			sampleType = InjectableSampleType.PCM8
		elif (nSampleWidth == 2):
			sampleType = InjectableSampleType.PCM16
		else:
			PrintAsFailure('ERROR: sample width is {}: only 1 or 2 is supported.'.format(nSampleWidth))
			raise ValueError('Invalid sample width')

		nFrameRate = audioObject.getframerate()

		nFrames = audioObject.getnframes()

		flDuration = nFrames / nFrameRate

		dataChunk = audioObject.readframes(nFrames)

		loopStart = 0
		loopEnd = 0

		# There might be loop information in the 'smpl' chunk, but we can't get there using 
		# the default Python 'wave' library.
		if fileType == "AIFF":
			# Note that this check is probably redundant since Python doesn't seem to support any of the AIFC I've run into
			compType = audioObject.getcomptype()
			if (compType != b'NONE'):
				PrintAsFailure('ERROR: Only uncompressed AIFF is supported.  This file is using {}.'.format(compType))
				raise ValueError('Invalid compression type')

			# use the loop information if present
			markerList = audioObject.getmarkers()

			if markerList:
				for marker in markerList:
					# Note we're using named keys because marker id values are arbitrary.
					if (marker[2] == b'beg loop'):
						loopStart = marker[1]
					elif (marker[2] == b'end loop'):
						loopEnd = marker[1]
		else: # WAV
			if (nSampleWidth == 1):
				# Unknown XOR as noted below
				fixedAudio = bytearray(len(dataChunk))
				for iPos, iCurr in enumerate(dataChunk):
					fixedAudio[iPos] = dataChunk[iPos] ^ 0x80
				dataChunk = fixedAudio

		print ('Requesting to inject {} file \"{}\":\n\tChannels {} SampWidth {} Framerate {} Count frames {} Duration {:2.2}s\n'.format(fileType, importFilename, nChannel, nSampleWidth, nFrameRate, nFrames, flDuration) )

		sampleList.append(InjectableSample(samplePosition, sampleType, dataChunk, nFrameRate, audioChannelsFlag, loopStart, loopEnd))

	except:
		PrintAsFailure('ERROR: Unable to load this %s file.' % (fileType))
		isValid = False

	audioObject.close()

	return isValid

# ==============================================================================================
class InjectableTrack:
	def __init__(self, groupDestination, referencedSPD):
		self.groupDestination = groupDestination
		self.referencedSPD = referencedSPD

# ==============================================================================================
class InjectableSPD:
	def __init__(self, targetRate, targetSample):
		self.targetRate = targetRate
		self.targetSample= targetSample

# ==============================================================================================
# Display data in the DTPK file
def dtpkparse(dtpkFileName, outFileName, changedGroups, changedSPDs, changedSamples):
	try:
		dtpkbytes = array('B', open(dtpkFileName, 'rb').read())
	except:
		PrintAsFailure('ERROR: \'%s\' could not be opened.' % dtpkFileName)
		sys.exit(0)

	if (dtpkbytes[:4].tobytes() != b'DTPK'):
		if dtpkbytes[:4].tobytes() == b'xobx':
			PrintAsFailure ('Skipping %s - XBox DTPKs are not supported' % dtpkFileName )
		else:
			PrintAsFailure ('Skipping %s - not a valid DTPK sound file' % dtpkFileName )
		return

	fIsShowingAnyInfo = show_detailed_file_info or show_header_info or show_unknown_chunk_data or  show_data_tracks or show_detailed_data_tracks or show_detailed_combination_data or show_program_defs_data
	fIsShowingAnyInfo = fIsShowingAnyInfo or show_lost_tracks or show_detailed_data_playback or show_detailed_data_effects or show_detailed_data_samples or show_detailed_data_samples_full
	fIsProbablyMvC2CharacterFile =  (dtpkFileName.upper().startswith("PL") and dtpkFileName.upper().endswith("_VOI.BIN"))
	filesize_actual = len(dtpkbytes)
	(filename_base, filename_ext) = os.path.splitext(dtpkFileName)

	# Header size is 0x60 for all understood files
	firstPostHeaderChunk = unpack('<I', dtpkbytes[0x20:0x24])[0]
	if (firstPostHeaderChunk == 0):
		PrintAsFailure('ERROR: This file is missing an expected chunk indicated at 0x20.  We should look into this file.')
		return
	elif (firstPostHeaderChunk != 0x60):
		# VTennis-NAOMI soundpack 3 does weird things
		PrintAsWarning('Warning: header is unexpectedly structured: length is 0x%02x not the usual 0x60.' % (firstPostHeaderChunk))

	chunkHeader = DTPKHeaderChunk(dtpkbytes[0:firstPostHeaderChunk])

	chunkHeader.WarnIfFileSizeValueIsWrong(filesize_actual)

	# Combination chunk: structured chunk.  2 bytes count entries, then 2 bytes relative offset for each entry, then data
	chunkCombinationData = DTPKCombinationChunk(dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Combination):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Combination) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Combination)])
	# Program definitions chunk: usually 0x98 bytes.  Often (not always) the same crc32 for the project
	chunkProgramDefsData = DTPKProgramDefsChunk(dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Program):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Program) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Program)])
	# Unknown chunk: 0x84 bytes long and crc32 of 0x44b0fc8f for all understood files.
	chunkUnknown1Data = DTPKUnhandledChunk(dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Unknown):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Unknown) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Unknown)])
	chunkSequencer = DTPKSequencerChunk(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Sequencer), dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Sequencer):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Sequencer) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Sequencer)])
	chunkPlaybackData = DTPKPlaybackDataChunk(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Playback), dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Playback):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Playback) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Playback)])
	chunkEffectsData = DTPKEffectsChunk(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Effects), dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Effects):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Effects) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Effects)])
	chunkSampleData = DTPKSampleDataChunk(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples), dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples) + chunkHeader.GetChunkLengthForRegion(DTPKRegions.Samples)])

	# Note that we look up sample count a little early so we know how many sample metadata entries we will need to read
	if chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples) != 0:
		count_samples = unpack('<I', dtpkbytes[chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples):chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Samples) + 4])[0] + 1
	else:
		count_samples = 0

	countPCMSamples = chunkSampleData.GetCountPCMSamples()
	countADPCMSamples = chunkSampleData.GetCountADPCMSamples()

	if show_header_info:
		print ('Analyzing %s:' % (dtpkFileName))
		chunkHeader.Print()

	if show_detailed_combination_data:
		chunkCombinationData.Print(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Combination))

	if show_program_defs_data:
		chunkProgramDefsData.Print(chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Program))

	if show_unknown_chunk_data:
		chunkUnknown1Data.Print(1, chunkHeader.GetChunkOffsetForRegion(DTPKRegions.Unknown))

	if show_data_tracks or show_detailed_data_tracks:
		chunkSequencer.Print()
	if show_lost_tracks:
		chunkSequencer.PrintLostTracks()
	if show_detailed_data_playback:
		chunkPlaybackData.Print()
	if show_detailed_data_effects:
		chunkEffectsData.Print()
	if show_detailed_data_samples:
		chunkSampleData.Print()

	chunkSampleData.AssignFirstSequencerEntries(chunkSequencer, chunkPlaybackData)

	countADPCMExports = 0
	countAIFFExports = 0
	countWAVExports = 0
	if (dump_adpcm_samples):
		countADPCMExports = chunkSampleData.ExtractIfYADPCM(filename_base)
	if (dump_pcm_samples_to_aiff):
		countAIFFExports = chunkSampleData.ExtractPCMToAIFF(filename_base)
	if (dump_pcm_samples_to_wav):
		countWAVExports = chunkSampleData.ExtractPCMToWAV(filename_base)

	if (reassemble_mode or (len(changedSamples) != 0) or (len(changedSPDs) != 0 ) or (len(changedGroups) != 0 )):
		chunkSequencer.HandleInjectedTracks(changedGroups)
		chunkPlaybackData.HandleInjectedSPDs(changedSPDs)
		chunkPlaybackData.HandleInjectedSamples(changedSamples)
		chunkSampleData.HandleInjectedSamples(changedSamples)

		testFileChunk = array('B', chunkHeader.GetBytes())
		testFileChunk += array('B', chunkCombinationData.GetBytes())
		testFileChunk += array('B', chunkProgramDefsData.GetBytes())
		newOffsetUnknown3Chunk = len(testFileChunk)
		testFileChunk += array('B', chunkUnknown1Data.GetBytes())
		newOffsetSequencerChunk = len(testFileChunk)
		testFileChunk += array('B', chunkSequencer.GetBytes())
		newOffsetPlaybackChunk = len(testFileChunk)
		testFileChunk += array('B', chunkPlaybackData.GetBytes())
		newOffsetEffectsChunk = len(testFileChunk)
		if (newOffsetPlaybackChunk == newOffsetEffectsChunk):
			# if we have a null section, the standard is to not reference
			newOffsetPlaybackChunk = 0
		testFileChunk += array('B', chunkEffectsData.GetBytes())
		newOffsetSampleChunk = len(testFileChunk)
		if (newOffsetSampleChunk == newOffsetEffectsChunk):
			# if we have a null section, the standard is to not reference
			newOffsetEffectsChunk = 0
		testFileChunk += array('B', chunkSampleData.GetBytes(len(testFileChunk)))
		
		print ('\nReassembling:')
		UpdateDTPKChunkOffsets(testFileChunk, newOffsetUnknown3Chunk, newOffsetSequencerChunk, newOffsetPlaybackChunk, newOffsetEffectsChunk, newOffsetSampleChunk, fIsProbablyMvC2CharacterFile)

		open(outFileName, "wb").write(testFileChunk)

		print ('\tFile \'%s\' created.  0x%x bytes.' % (outFileName, len(testFileChunk)))

	if ((not fIsShowingAnyInfo) and (len(outFileName) == 0) and (not dump_adpcm_samples) and (not dump_pcm_samples_to_aiff) and (not dump_pcm_samples_to_wav)):
		print ('\n"%s" is a valid DTPK file.  It contains %u command groups, a total of %u tracks, and uses %u samples (%u PCM, %u ADPCM).' % (dtpkFileName, chunkSequencer.GetGroupCount(), chunkSequencer.GetTotalTrackCount(), count_samples, countPCMSamples, countADPCMSamples))
		print ('\tThis script can display detailed file information and/or extract out the samples.')
		print ('\tUse -options to see the current list of options.')
		print ('')

	if (show_detailed_file_info):
		print ('%u ADPCM samples found' % countADPCMSamples)
	if (countADPCMExports != 0):
		print ('Extracted out %u ADPCM samples' % countADPCMExports)
	elif (dump_adpcm_samples):
		print ('No ADPCM samples found.')

	if (show_detailed_file_info):
		print ('%u PCM samples found' % (countPCMSamples))
	if (countAIFFExports != 0):
		print ('Extracted out %u PCM samples to AIFF' % countAIFFExports)
	elif (dump_pcm_samples_to_aiff):
		print ('No PCM samples found to export to AIFF.')
	if (countWAVExports != 0):
		print ('Extracted out %u PCM samples to WAV' % countWAVExports)
	elif (dump_pcm_samples_to_wav):
		print ('No PCM samples found to export to WAV.')

# ==============================================================================================

def PrintUsage(show_full):
	print ('\nAM2 DTPK info script v0.011')
	print ('Usage: python DTPKDump.py [options] <DTPK sound file>')
	print ('Options:')
	print ('          -showinfo         display detailed information about all DTPK contents')
	print ('          -extractall       extract out all samples')
	print ('          -wavconv          extract out all samples as PCM WAV files')
	print ('                            (This options uses Sappharad\'s ADPCM to PCM conversion code.)')
	print ('          -spliceadpcm (NUM) (FREQUENCY) (FILENAME)  replace sample NUM in the DTPK with the specified ADPCM file')
	print ('                            Requires -out')
	print ('          -spliceaiff (NUM) (FILENAME)  replace sample NUM in the DTPK with the specified AIFF file')
	print ('                            AIFF files must be uncompressed 8bit or 16bit')
	print ('                            Requires -out')
	print ('          -splicepcm (NUM) (FILENAME)  replace sample NUM in the DTPK with the specified PCM WAV file')
	print ('                            WAV files must be PCM 8bit or 16bit')
	print ('                            Requires -out')
	print ('          -addtrack (GROUP) (SPD) add a new track to GROUP using SPD')
	print ('                            The GROUP value can be either absolute index or group descriptor (ex: a920)')
	print ('                            Use special SPD value \"0xbad\" if you want to inject a NULL/empty track')
	print ('                            Requires -out')
	print ('          -addplayback (NUM) (FREQUENCY) add a new SPD using sample NUM played back at FREQUENCY')
	print ('                            Requires -out')
	print ('          -out (FILENAME)   save the updated DTPK file as FILENAME')
	print ('                            Requires one of the splicing options.')
	print ('          -options          shows the expanded list of options')
	if (show_full):
		print ('          -showheaderinfo   display detailed information about the header')
		print ('          -showcombinationinfo  display detailed information about the combination data')
		print ('          -showprogramdefsinfo  display basic information about the program definitions data')
		print ('          -showunknowninfo  display basic information about the unknown table')
		print ('          -showtrackinfo    display information about the track data')
		print ('          -showfulltrackinfo display detailed information about the track data')
		#print ('          -showlosttracks   display information about tracks that might have been extracted incompletely ')
		print ('          -showplaybackinfo display detailed information about the sample playback data')
		print ('                            Note that we do not show stock settings by default: use -v if you want those too')
		print ('          -showeffectsinfo  display detailed information about the sample effects data')
		print ('          -showsampleinfo   display detailed information about the samples')
		print ('          -v                if showing Playback data show all data even if using stock values')
		print ('          -dumpadpcm        extract out all ADPCM samples to raw Yamaha ADPCM files (usable in Awave Studio)')
		print ('          -dumpaiff         extract out all PCM samples to PCM AIFF files (may not work for strange playback rates)')
		print ('          -dumpaiffall      extract out *all* samples to PCM AIFF files (may not work for strange playback rates)')
		print ('          -dumppcm          extract out all PCM samples to PCM WAV files (may not work for strange playback rates)')
		print ('          -dumppcmall       extract out *all* samples to PCM WAV files (may not work for strange playback rates)')
		print ('          -reassemble       rebuild the file (to test reassembly logic)')
		print ('                            Requires -out')

	
# ==============================================================================================
if __name__ == '__main__':
	argv = sys.argv
	argc = len(argv)
	os.system('color')

	fFailedArgProcessing = False
	dtpkFilename = ""
	outFile = ""

	updatedGroups = []
	updatedSPDs = []
	updatedSamples = []

	shouldShowFullOptions = False

	if (argc > 1):
		iCurrArg = 1
		while iCurrArg <= len(argv[1:]):
			if (argv[iCurrArg][0] == '-'):
				if (argv[iCurrArg] == "-showinfo"):
					show_detailed_file_info = True
					show_header_info = True
					show_detailed_combination_data = True
					show_program_defs_data = True
					show_unknown_chunk_data = True
					show_data_tracks = True
					show_detailed_data_tracks = True
					show_detailed_data_playback = True
					show_detailed_data_samples = True
					show_detailed_data_samples_full = True
				elif (argv[iCurrArg] == "-showheaderinfo"):
					show_header_info = True
				elif (argv[iCurrArg] == "-showcombinationinfo"):
					show_detailed_combination_data = True
				elif (argv[iCurrArg] == "-showprogramdefsinfo"):
					show_program_defs_data = True
				elif (argv[iCurrArg] == "-showunknowninfo"):
					show_unknown_chunk_data = True
				elif (argv[iCurrArg] == "-showtrackinfo"):
					show_data_tracks = True
				elif (argv[iCurrArg] == "-showfulltrackinfo"):
					show_detailed_data_tracks = True
				elif (argv[iCurrArg] == "-showlosttracks"):
					show_lost_tracks = True
				elif (argv[iCurrArg] == "-showplaybackinfo"):
					show_detailed_data_playback = True
				elif (argv[iCurrArg] == "-showeffectsinfo"):
					show_detailed_data_effects = True
				elif (argv[iCurrArg] == "-showsampleinfo"):
					show_detailed_data_samples = True
					show_detailed_data_samples_full = True
				elif ((argv[iCurrArg] == "-dump") or (argv[iCurrArg] == "-dumpall") or (argv[iCurrArg] == "-extractall")):
					dump_adpcm_samples = True
					dump_pcm_samples_to_wav = True
				elif (argv[iCurrArg] == "-dumpadpcm"):
					dump_adpcm_samples = True
				elif (argv[iCurrArg] == "-dumpaiff"):
					dump_pcm_samples_to_aiff = True
				elif (argv[iCurrArg] == "-dumpaiffall"):
					dump_pcm_samples_to_aiff = True
					dump_adpcm_samples_to_aiff = True
				elif (argv[iCurrArg] == "-dumppcm"):
					dump_pcm_samples_to_wav = True
				elif ((argv[iCurrArg] == "-dumppcmall") or (argv[iCurrArg] == "-wavconv")):
					dump_pcm_samples_to_wav = True
					dump_adpcm_samples_to_wav = True
				elif (argv[iCurrArg] == "-spliceadpcm"):
					iCurrArg += 1
					try:
						argSamplePosition = int(argv[iCurrArg])
					except:
						try:
							argSamplePosition = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what sample slot to inject this sample into.\n')
							fFailedArgProcessing = True
							break
					iCurrArg += 1
					try:
						argSampleRate = int(argv[iCurrArg])
					except:
						try:
							argSampleRate = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what sample slot to inject this sample into.\n')
							fFailedArgProcessing = True
							break
					iCurrArg += 1
					try:
						adpcmFile = argv[iCurrArg]
					except:
						PrintAsFailure('ERROR: check usage.  You need to tell us what ADPCM file to splice in.')
						fFailedArgProcessing = True
						break

					if (not ImportAndAddADPCMFile(adpcmFile, updatedSamples, argSamplePosition, argSampleRate)):
						PrintAsFailure('ERROR: file \'%s\' could not be imported.' % adpcmFile)
						fFailedArgProcessing = True
						break
				elif (argv[iCurrArg] == "-reassemble"):
					reassemble_mode = True
				elif (argv[iCurrArg] == "-v"):
					fVerboseOutput = True
				elif (argv[iCurrArg] == "-spliceaiff"):
					iCurrArg += 1
					try:
						argSamplePosition = int(argv[iCurrArg])
					except:
						try:
							argSamplePosition = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what sample slot to inject this sample into.\n')
							fFailedArgProcessing = True
							break
					iCurrArg += 1
					try:
						aiffFile = argv[iCurrArg]
					except:
						PrintAsFailure('ERROR: check usage.  You need to tell us what AIFF file to splice in.')
						fFailedArgProcessing = True
						break
					
					if (not ImportAndAddAudioFile("AIFF", aiffFile, updatedSamples, argSamplePosition)):
						PrintAsFailure('ERROR: file \'%s\' could not be imported.' % aiffFile)
						fFailedArgProcessing = True
						break
				elif (argv[iCurrArg] == "-splicepcm"):
					iCurrArg += 1
					try:
						argSamplePosition = int(argv[iCurrArg])
					except:
						try:
							argSamplePosition = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what sample slot to inject this sample into.\n')
							fFailedArgProcessing = True
							break
					iCurrArg += 1
					try:
						wavFile = argv[iCurrArg]
					except:
						PrintAsFailure('ERROR: check usage.  You need to tell us what WAV file to splice in.')
						fFailedArgProcessing = True
						break
					
					if (not ImportAndAddAudioFile("WAV", wavFile, updatedSamples, argSamplePosition)):
						PrintAsFailure('ERROR: file \'%s\' could not be imported.' % (wavFile))
						fFailedArgProcessing = True
						break
				elif (argv[iCurrArg] == "-addtrack"):
					iCurrArg += 1
					try:
						argTargetGroup = int(argv[iCurrArg])
					except:
						try:
							argTargetGroup = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what group to inject this sample into and what SPD it should point to.')
							PrintAsFailure('The value of the group parameter can either be the absolute group index or the group descriptor (ex: A920)')
							fFailedArgProcessing = True
							break
					iCurrArg += 1
					try:
						argTargetSPD = int(argv[iCurrArg])
					except:
						try:
							argTargetSPD = int(argv[iCurrArg], 16)
						except:
							PrintAsFailure('ERROR: check usage.  You need to tell us what group to inject this sample into and what SPD it should point to.\n')
							fFailedArgProcessing = True
							break
					updatedGroups.append(InjectableTrack(argTargetGroup, argTargetSPD))
				elif (argv[iCurrArg] == "-addplayback"):
					iCurrArg += 1
					try:
						argTargetSample = int(argv[iCurrArg])
						iCurrArg += 1
						argTargetRate = int(argv[iCurrArg])
						updatedSPDs.append(InjectableSPD(argTargetRate, argTargetSample))
					except:
						PrintAsFailure('ERROR: check usage.  To add a playback we need to know what rate and sample to use.\n')
						fFailedArgProcessing = True
						break
				elif ((argv[iCurrArg] == "-o") or (argv[iCurrArg] == "-out")):
					iCurrArg += 1
					outFile = argv[iCurrArg]
				elif ((argv[iCurrArg] == "-options") or (argv[iCurrArg] == "-?")):
					shouldShowFullOptions = True
					break
				else:
					PrintAsFailure('Error: unsupported option %s' % (argv[iCurrArg]))
					shouldShowFullOptions = True
					fFailedArgProcessing = True
					break
			else:
				dtpkFilename = argv[iCurrArg]
			iCurrArg += 1

	fIsChangingComposition = (len(updatedSamples) != 0) or (len(updatedSPDs) != 0 ) or (len(updatedGroups) != 0 )
	if (fIsChangingComposition and (len(outFile) == 0)):
		PrintAsFailure('ERROR: you must specify an output filename if trying to splice in a sample.\n')
		fFailedArgProcessing = shouldShowFullOptions = True
	elif (reassemble_mode and (len(outFile) == 0)):
		PrintAsFailure('ERROR: you must specify an output filename if testing file reassembly.\n')
		fFailedArgProcessing = shouldShowFullOptions = True
	elif ((len(outFile) != 0) and ((not fIsChangingComposition) and (not reassemble_mode))):
		PrintAsFailure('ERROR: output file specified with no usable samples to splice in.\n')
		fFailedArgProcessing = shouldShowFullOptions = True

	if ((len(dtpkFilename) != 0) and (not fFailedArgProcessing)):
		dtpkparse(dtpkFilename, outFile, updatedGroups, updatedSPDs, updatedSamples)

		if warningCount:
			PrintAsWarning('WARNING: %u warnings encountered.' % (warningCount))
		if errorCount:
			PrintAsFailure('ERROR: %u errors encountered.' % (errorCount))
	else:
		PrintUsage(shouldShowFullOptions)
		sys.exit(0)

# ==============================================================================================

# ==============================================================================================
# 24-06-07 (0.011)   - Beta ability to add tracks and/or SPDs
# 24-05-31 (0.010)   - Stateful tracking of file data
# 24-05-18 (0.009)   - Mapped out more of the DTPK structure, associated better handling
# 24-04-25 (0.008)   - Improve handling of sequencer groups
# 24-04-20 (0.007)   - Allow exporting YADPCM chunks as RIFF/WAV PCM
# 24-04-16 (0.006)   - Significantly improve rate handling
# 24-04-13 (0.005)   - Efficiency: always inline sample replacement
# 24-04-10 (0.004)   - Support splicing in ADPCM chunks or PCM WAV files
# 24-04-03 (0.003)   - Support slightly limited PCM export to WAV
# 24-03-27 (0.002)   - Smooth out T->SPD->S logic, filenames, etc.
# 24-03-21 (0.001)   - Add optional sample extraction
# 24-03-01 (0.000)   - Initial version based upon KingShriek and Shenmue's knowledge.
# ==============================================================================================
