"""Gameplay settings the UI exposes, and where each one lives on disk.

Each row: (label, key, help, type, file, section, default).
type is "bool", "number" or "scarcity"; file is "Engine" or "Game".

Names and defaults are checked against the game itself, not against guides. At every start the
dedicated server logs each of its "Vein settings" and where the value came from:

    Initializing Vein setting GS_ZombiesHealth
        Initialized from cvar (vein.Zombies.Health = 40.000000)
    Initializing Vein setting GS_HungerMultiplier
        Initialized from default value (1.000000)

With nothing overridden those values are the defaults (last checked on build 25708939). Most
settings are backed by a console variable and go in Engine.ini under [ConsoleVariables]; the few
that are not ("default value" above) go in Game.ini under [/Script/Vein.ServerSettings] by their
GS_ name, as Ramjet's Config page describes for GS_ShowScoreboardBadges.

A console variable the server does not log there (the XP multiplier, for one) is still real when
Ramjet's Console Variable List names it; that list says of itself that it is incomplete and dated.
"""
from __future__ import annotations

CVARS = "ConsoleVariables"
SERVER_SETTINGS = "/Script/Vein.ServerSettings"

SCARCITY_OPTIONS = [
    ("0.0", "Infinite"),
    ("1.0", "More Loot"),
    ("2.0", "Standard"),
    ("3.0", "Less Loot"),
    ("4.0", "Impossible"),
]

GAMEPLAY_DEFINITIONS = {
    "General & Loot": [
        ("Loot Scarcity", "vein.Scarcity.Difficulty", "Loot rarity. Standard is 2.0.", "scarcity", "Engine", CVARS, "2.0"),
        ("Max Characters", "vein.Characters.Max", "Characters per SteamID.", "number", "Engine", CVARS, "100"),
        ("Containers Respawn", "vein.ContainersRespawn.Enabled", "Chests refill over time.", "bool", "Engine", CVARS, True),
        ("World Items Respawn", "vein.ItemActorSpawner.Respawns", "Items on shelves respawn.", "bool", "Engine", CVARS, True),
        ("Furniture Respawns", "vein.Furniture.Respawns", "Destroyed doors and tables return.", "bool", "Engine", CVARS, True),
        ("Furniture Respawn Rate", "vein.Furniture.RespawnRate", "Cooldown in seconds (higher is slower).", "number", "Engine", CVARS, "900.0"),
        ("Max Utility Cabinets", "vein.Placement.MaxUtilityCabinets", "Limit per area. 0 is unlimited.", "number", "Engine", CVARS, "0"),
        ("Wire Max Radius", "vein.Wire.MaxRadius", "Electrical wire range.", "number", "Engine", CVARS, "1500"),
        ("TV Max Radius", "vein.Wire.TVMaxRadius", "Electrical range for TVs.", "number", "Engine", CVARS, "5000"),
        ("Hideable Clothes", "vein.ClothingHideable", "Armor can be hidden.", "bool", "Engine", CVARS, False),
        ("TVs Play Online Video", "vein.TV.Server.AllowRemoteContent", "TVs may play content from the internet.", "bool", "Engine", CVARS, True),
        ("Ignore Placement Rules", "vein.Placement.IgnorePlacementRestrictions", "Build without the usual placement restrictions.", "bool", "Engine", CVARS, False),
    ],
    "Players": [
        ("Walk Speed", "vein.Movement.WalkSpeed", "Player walking speed.", "number", "Engine", CVARS, "250"),
        ("Sprint Extra Speed", "vein.Movement.RunExtraSpeed", "Speed added on top of walking when sprinting.", "number", "Engine", CVARS, "350"),
        ("Crouch Speed", "vein.Movement.CrouchWalkSpeed", "Player speed while crouched.", "number", "Engine", CVARS, "200"),
        ("Swim Speed", "vein.Movement.SwimSpeed", "Player swimming speed.", "number", "Engine", CVARS, "300"),
        ("Keep Items On Death", "vein.RetainItemsOnDeath", "Players keep their inventory when they die.", "bool", "Engine", CVARS, False),
        ("Persistent Corpses", "vein.PersistentCorpses", "Bodies stay in the world.", "bool", "Engine", CVARS, True),
        ("Stun Duration", "vein.StunLockDuration", "Seconds a stun lasts.", "number", "Engine", CVARS, "2.0"),
        ("Max Third-Person Distance", "GS_MaxThirdPersonDistance", "How far the third-person camera may sit from the player.", "number", "Game", SERVER_SETTINGS, "400"),
        ("Scoreboard Admin Badges", "GS_ShowScoreboardBadges", "Show badges for admins and super admins on the scoreboard.", "bool", "Game", SERVER_SETTINGS, True),
    ],
    "Survival & Time": [
        ("Time Multiplier", "vein.Time.TimeMultiplier", "Day speed (higher is faster days).", "number", "Engine", CVARS, "16.0"),
        ("Night Multiplier", "vein.Time.NightTimeMultiplier", "Night speed (higher is faster nights).", "number", "Engine", CVARS, "3.0"),
        ("Night Start Hour", "vein.Time.NightTimeMultiplierStart", "Hour night begins (24h).", "number", "Engine", CVARS, "20.0"),
        ("Night End Hour", "vein.Time.NightTimeMultiplierEnd", "Hour night ends (24h).", "number", "Engine", CVARS, "6.0"),
        ("Time Passes When Empty", "vein.Time.ContinueWithNoPlayers", "Time runs with nobody online.", "bool", "Engine", CVARS, False),
        ("Sleeping Advances Time", "vein.Sleep.AdvanceTime", "Sleeping moves the clock forward.", "bool", "Engine", CVARS, False),
        ("Hunger Multiplier", "GS_HungerMultiplier", "Drain speed (higher starves faster).", "number", "Game", SERVER_SETTINGS, "1.0"),
        ("Thirst Multiplier", "GS_ThirstMultiplier", "Drain speed (higher dehydrates faster).", "number", "Game", SERVER_SETTINGS, "1.0"),
        ("Tiredness Multiplier", "GS_TiredMultiplier", "How fast players get tired (higher is faster).", "number", "Game", SERVER_SETTINGS, "1.0"),
        ("Global XP Multiplier", "vein.Stats.XPMultiplier", "XP gain (2.0 is double XP).", "number", "Engine", CVARS, "1.0"),
        ("Start Offset Days", "vein.Time.StartOffsetDays", "Days already passed at start.", "number", "Engine", CVARS, "0"),
        ("Electricity Shutoff Day", "vein.Calendar.ElectricalShutoffTimeDays", "Day the grid fails.", "number", "Engine", CVARS, "46"),
        ("Water Shutoff Day", "vein.Calendar.WaterShutoffTimeDays", "Day the water fails.", "number", "Engine", CVARS, "30"),
    ],
    "Zombies": [
        ("Zombie Health", "vein.Zombies.Health", "Base HP.", "number", "Engine", CVARS, "40"),
        ("Headshots Only", "vein.Zombies.HeadshotOnly", "Only headshots kill.", "bool", "Engine", CVARS, False),
        ("Spawn Density", "vein.AISpawner.SpawnCapMultiplierZombie", "Higher is more zombies.", "number", "Engine", CVARS, "1.0"),
        ("Run Speed Multiplier", "vein.Zombies.RunSpeedMultiplier", "Sprinter speed.", "number", "Engine", CVARS, "1.0"),
        ("Walk Speed Multiplier", "vein.Zombies.WalkSpeedMultiplier", "Walker speed.", "number", "Engine", CVARS, "1.0"),
        ("Crawl Speed Multiplier", "vein.Zombies.CrawlSpeedMultiplier", "Crawler speed.", "number", "Engine", CVARS, "1.0"),
        ("Global Speed Multiplier", "vein.Zombies.SpeedMultiplier", "Speed of all zombies.", "number", "Engine", CVARS, "1.0"),
        ("Horde Mode", "vein.AISpawner.Hordes.Enabled", "Roaming hordes.", "bool", "Engine", CVARS, True),
        ("Always Turn", "vein.AlwaysBecomeZombie", "Players turn on death.", "bool", "Engine", CVARS, False),
        ("Can Climb", "vein.Zombies.CanClimb", "Zombies climb walls.", "bool", "Engine", CVARS, True),
        ("Infection Chance", "vein.ZombieInfectionChance", "Probability per hit (0.01 is 1%).", "number", "Engine", CVARS, "0.01"),
        ("Damage Multiplier", "vein.Zombies.DamageMultiplier", "Zombie damage output.", "number", "Engine", CVARS, "1.0"),
        ("Hearing Multiplier", "vein.Zombies.HearingMultiplier", "Detection range.", "number", "Engine", CVARS, "1.0"),
        ("Sight Multiplier", "vein.Zombies.SightMultiplier", "Vision range.", "number", "Engine", CVARS, "1.0"),
        ("Stagger Chance", "vein.StaggerChance", "Chance per hit (0.1 is 10%).", "number", "Engine", CVARS, "0.1"),
        ("Stun Chance", "vein.StunLockChance", "Chance per hit (0.6 is 60%).", "number", "Engine", CVARS, "0.6"),
        ("Walker Percentage", "vein.AISpawner.ZombieWalkerPercentage", "0.8 is 80% walkers.", "number", "Engine", CVARS, "0.8"),
    ],
    "PvP & Vehicles": [
        ("Enable PvP", "vein.PvP", "Player versus player combat.", "bool", "Engine", CVARS, True),
        ("Vehicle Player Damage", "vein.Vehicles.Damage.OutgoingPlayerDamage", "Collision damage (higher is more).", "number", "Engine", CVARS, "1.0"),
        ("Permadeath", "vein.Permadeath", "Character deleted on death.", "bool", "Engine", CVARS, False),
        ("Iron Man Mode", "vein.NoSaves", "No manual saves allowed.", "bool", "Engine", CVARS, False),
        ("Base Damage", "vein.BaseDamage", "Bases can be damaged.", "bool", "Engine", CVARS, True),
        ("Player Raid Base", "vein.BuildObjectPvP", "Players can damage bases.", "bool", "Engine", CVARS, True),
        ("Structure Decay", "vein.BuildObjectDecay", "Abandoned structures decay.", "bool", "Engine", CVARS, True),
        ("Decay Interval", "vein.UtilityCabinet.Interval", "Hours between decay ticks.", "number", "Engine", CVARS, "4.0"),
        ("Raid Utility Cabinets", "vein.UtilityCabinet.AllowRaiding", "Utility cabinets can be raided.", "bool", "Engine", CVARS, True),
        ("Max Pylons", "vein.UtilityCabinet.MaxPylons", "Pylons per utility cabinet.", "number", "Engine", CVARS, "6"),
        ("Max Pylon Distance", "vein.UtilityCabinet.MaxPylonDistance", "How far a pylon may be from its cabinet.", "number", "Engine", CVARS, "5000"),
        ("Offline Protection", "vein.OfflineRaidProtection", "Reduce damage to offline players' bases.", "bool", "Engine", CVARS, False),
        ("Pickpocketing", "vein.AllowPickpocketing", "Players can steal from inventories.", "bool", "Engine", CVARS, True),
        ("Headshot Multiplier", "vein.HeadshotDamageMultiplier", "PvP headshot damage multiplier.", "number", "Engine", CVARS, "2.0"),
    ],
}


def schema() -> list[dict]:
    return [
        {
            "category": category,
            "settings": [
                {
                    "label": label,
                    "key": key,
                    "help": help_,
                    "type": type_,
                    "default": default,
                    **({"options": [{"value": v, "label": f"{n} ({v})"} for v, n in SCARCITY_OPTIONS]} if type_ == "scarcity" else {}),
                }
                for label, key, help_, type_, _file, _section, default in rows
            ],
        }
        for category, rows in GAMEPLAY_DEFINITIONS.items()
    ]


def rows():
    for settings in GAMEPLAY_DEFINITIONS.values():
        yield from settings
