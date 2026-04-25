package edu.sub;

import edu.sub.proto.CanUpdate;
import edu.sub.proto.MessageCategory;
import edu.sub.proto.MessageInfo;
import edu.sub.proto.Signal;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

public class Catalog {

    static class Sig {
        String name;
        String unit;
        double min;
        double max;
        Sig(String n, String u, double mn, double mx) { name = n; unit = u; min = mn; max = mx; }
    }

    static class Msg {
        String name;
        MessageCategory cat;
        List<Sig> signals;
        Msg(String n, MessageCategory c, List<Sig> s) { name = n; cat = c; signals = s; }
    }

    private static final List<Msg> ALL = new ArrayList<>();
    private static final Random RAND = new Random();

    static {
        ALL.add(new Msg("BMSMasterStatus", MessageCategory.BMS, List.of(
                new Sig("pack_voltage", "V", 250, 420),
                new Sig("pack_current", "A", -200, 200),
                new Sig("soc", "%", 0, 100))));
        ALL.add(new Msg("BMSCellVoltages", MessageCategory.BMS, List.of(
                new Sig("min_v", "V", 3.0, 4.2),
                new Sig("max_v", "V", 3.0, 4.2))));
        ALL.add(new Msg("BMSTemperature", MessageCategory.BMS, List.of(
                new Sig("min_t", "C", 15, 60),
                new Sig("max_t", "C", 15, 60))));

        ALL.add(new Msg("EngineRPM", MessageCategory.ENGINE, List.of(
                new Sig("rpm", "", 0, 18000),
                new Sig("torque", "Nm", 0, 250))));
        ALL.add(new Msg("EngineState", MessageCategory.ENGINE, List.of(
                new Sig("state", "", 0, 3))));

        ALL.add(new Msg("ChargerPower", MessageCategory.CHARGER, List.of(
                new Sig("power_kw", "kW", 0, 150),
                new Sig("voltage", "V", 250, 420))));
        ALL.add(new Msg("ChargerStatus", MessageCategory.CHARGER, List.of(
                new Sig("connected", "", 0, 1))));

        ALL.add(new Msg("CoolingPump", MessageCategory.COOLING, List.of(
                new Sig("flow_lpm", "L/min", 0, 30),
                new Sig("pressure", "bar", 0, 5))));
        ALL.add(new Msg("CoolingFan", MessageCategory.COOLING, List.of(
                new Sig("rpm", "", 0, 4000))));

        ALL.add(new Msg("HeadLights", MessageCategory.LIGHTS, List.of(
                new Sig("on", "", 0, 1),
                new Sig("high_beam", "", 0, 1))));
        ALL.add(new Msg("BrakeLights", MessageCategory.LIGHTS, List.of(
                new Sig("on", "", 0, 1))));

        ALL.add(new Msg("TirePressure", MessageCategory.SENSORS, List.of(
                new Sig("front_l", "bar", 1.5, 3.0),
                new Sig("front_r", "bar", 1.5, 3.0),
                new Sig("rear_l", "bar", 1.5, 3.0),
                new Sig("rear_r", "bar", 1.5, 3.0))));
        ALL.add(new Msg("ImuAccel", MessageCategory.SENSORS, List.of(
                new Sig("ax", "g", -2, 2),
                new Sig("ay", "g", -2, 2),
                new Sig("az", "g", -2, 2))));
        ALL.add(new Msg("GpsPosition", MessageCategory.SENSORS, List.of(
                new Sig("lat", "deg", 49.5, 50.5),
                new Sig("lon", "deg", 19.5, 20.5),
                new Sig("speed", "kmh", 0, 250))));

        ALL.add(new Msg("DashSpeed", MessageCategory.DASHBOARD, List.of(
                new Sig("speed_kmh", "kmh", 0, 250),
                new Sig("range_km", "km", 0, 500))));
        ALL.add(new Msg("DashWarnings", MessageCategory.DASHBOARD, List.of(
                new Sig("count", "", 0, 10))));

        ALL.add(new Msg("AcceleratorPedal", MessageCategory.PEDALS, List.of(
                new Sig("position", "%", 0, 100))));
        ALL.add(new Msg("BrakePedal", MessageCategory.PEDALS, List.of(
                new Sig("position", "%", 0, 100),
                new Sig("force", "N", 0, 1500))));

        ALL.add(new Msg("InverterPower", MessageCategory.POWER, List.of(
                new Sig("power_kw", "kW", -100, 250),
                new Sig("temp", "C", 20, 90))));
        ALL.add(new Msg("DcDcConverter", MessageCategory.POWER, List.of(
                new Sig("v_out", "V", 11, 14),
                new Sig("i_out", "A", 0, 60))));
        ALL.add(new Msg("AuxBattery", MessageCategory.POWER, List.of(
                new Sig("voltage", "V", 11, 14))));

        ALL.add(new Msg("NodeBmsAlive", MessageCategory.NODE_STATUS, List.of(
                new Sig("uptime_s", "s", 0, 100000))));
        ALL.add(new Msg("NodeEngineAlive", MessageCategory.NODE_STATUS, List.of(
                new Sig("uptime_s", "s", 0, 100000))));
        ALL.add(new Msg("NodeChargerAlive", MessageCategory.NODE_STATUS, List.of(
                new Sig("uptime_s", "s", 0, 100000))));
    }

    public static List<MessageInfo> listAll() {
        List<MessageInfo> out = new ArrayList<>();
        for (Msg m : ALL) {
            out.add(MessageInfo.newBuilder()
                    .setName(m.name)
                    .setCategory(m.cat)
                    .build());
        }
        return out;
    }

    public static boolean exists(String name) {
        for (Msg m : ALL) {
            if (m.name.equals(name)) return true;
        }
        return false;
    }

    public static CanUpdate generateRandom() {
        Msg m = ALL.get(RAND.nextInt(ALL.size()));
        CanUpdate.Builder b = CanUpdate.newBuilder()
                .setMessageName(m.name)
                .setCategory(m.cat)
                .setTimestampMs(System.currentTimeMillis());
        for (Sig s : m.signals) {
            double v = s.min + RAND.nextDouble() * (s.max - s.min);
            b.addSignals(Signal.newBuilder()
                    .setName(s.name)
                    .setValue(Math.round(v * 100.0) / 100.0)
                    .setUnit(s.unit)
                    .build());
        }
        return b.build();
    }
}
