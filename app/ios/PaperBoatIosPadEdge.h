// PaperBoatIosPadEdge.h — the shell touch pad's press/release publisher.
//
// PROBLEM (user report on 1.0.1, 2026-09-23): "button spamming just doesn't work.
// Freezes up. Easiest way to see is to spam hammer." The 0.0.0.x design kept a
// released bit published for a fixed 60 ms so that a tap shorter than one
// 30 Hz game tick could not fall between two samples. A NEW press inside that
// 60 ms simply re-set the bit, so a tap train with any gap under 60 ms was one
// continuous hold from the game's point of view — and Paper Mario reads a hold
// as ONE press. Spam froze the button until the player slowed down; upstream's
// engine-drawn pad (no hold-over) was fine.
//
// DESIGN. Per N64 bit, publish a state that always lasts at least one tick
// (PB_PAD_EDGE_MIN_S > 33.3 ms), and count presses instead of timing releases:
//   - UIKit bumps a per-bit press SEQUENCE on every finger-down and keeps the
//     live "held" word;
//   - the reader (game tick) flips its published bit only after the current
//     state has been up for the minimum, goes DOWN when there is a press it
//     has not published yet, and goes UP as soon as the press it is publishing
//     is no longer the one being held (released, or released-and-pressed-again).
// Every press therefore yields exactly one down window and one up window that
// the 30 Hz sampler cannot miss; presses faster than 1 / (2 * min) collapse
// into that rate rather than into a hold. Isolated presses still publish on
// the first read after the finger lands (latency unchanged). Pure C so the
// shell and a host-side test compile the same function.
#ifndef PAPERBOAT_IOS_PAD_EDGE_H
#define PAPERBOAT_IOS_PAD_EDGE_H

#define PB_PAD_EDGE_BITS 16
// 45 ms: one 30 Hz tick is 33.3 ms, so every published state spans at least
// one sample even with a few ms of tick jitter. Ceiling ~11 presses/s.
#define PB_PAD_EDGE_MIN_S 0.045

typedef struct {
    unsigned short published;               // bits the game currently sees
    double since[PB_PAD_EDGE_BITS];         // when each bit last changed
    unsigned int consumed[PB_PAD_EDGE_BITS]; // press seq behind each published DOWN
} PBPadEdgeState;

// held: bits under a finger right now. seq[i]: presses of bit i so far.
// Returns the word to publish. Reader-owned state; call from one thread.
static inline unsigned short PBPadEdge_Step(PBPadEdgeState* s, unsigned short held, const unsigned int* seq,
                                            double now, double minS) {
    for (int i = 0; i < PB_PAD_EDGE_BITS; i++) {
        const unsigned short bit = (unsigned short)(1u << i);
        if (now - s->since[i] < minS) {
            continue; // the current state has not been sampleable for a tick yet
        }
        const int down = (s->published & bit) != 0;
        const int fingerDown = (held & bit) != 0;
        const int samePress = seq[i] == s->consumed[i];
        if (down) {
            if (fingerDown && samePress) {
                continue; // a genuine hold
            }
            s->published = (unsigned short)(s->published & ~bit);
            s->since[i] = now;
        } else if (!samePress) {
            s->published |= bit; // a press nobody has seen yet (finger may already be up)
            s->consumed[i] = seq[i];
            s->since[i] = now;
        }
    }
    return s->published;
}

#endif // PAPERBOAT_IOS_PAD_EDGE_H
